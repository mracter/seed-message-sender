import json
import uuid
import logging
import responses

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.conf import settings
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token
from requests_testadapter import TestAdapter, TestSession
from go_http.metrics import MetricsApiClient
from go_http.send import LoggingSender

from .factory import (
    MessageClientFactory, JunebugApiSender, HttpApiSender,
    JunebugApiSenderException, FactoryException)
from .models import (Inbound, Outbound, fire_msg_action_if_new,
                     fire_metrics_if_new)
from .tasks import Send_Message, fire_metric
from . import tasks

from seed_message_sender.utils import load_callable

Send_Message.get_text_client = lambda x: LoggingSender('go_http.test')
Send_Message.get_voice_client = lambda x: LoggingSender('go_http.test')


class RecordingAdapter(TestAdapter):

    """ Record the request that was handled by the adapter.
    """
    request = None

    def send(self, request, *args, **kw):
        self.request = request
        return super(RecordingAdapter, self).send(request, *args, **kw)


class RecordingHandler(logging.Handler):

    """ Record logs. """
    logs = None

    def emit(self, record):
        if self.logs is None:
            self.logs = []

        self.logs.append(record)


class APITestCase(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.adminclient = APIClient()
        self.session = TestSession()


class AuthenticatedAPITestCase(APITestCase):

    def make_outbound(self):
        self._replace_post_save_hooks_outbound()  # don't let fixtures fire
        outbound_message = {
            "to_addr": "+27123",
            "vumi_message_id": "075a32da-e1e4-4424-be46-1d09b71056fd",
            "content": "Simple outbound message",
            "delivered": False,
            "attempts": 1,
            "metadata": {}
        }
        outbound = Outbound.objects.create(**outbound_message)
        self._restore_post_save_hooks_outbound()  # let tests fire tasks
        return str(outbound.id)

    def make_inbound(self, in_reply_to):
        inbound_message = {
            "message_id": str(uuid.uuid4()),
            "in_reply_to": in_reply_to,
            "to_addr": "+27123",
            "from_addr": "020",
            "content": "Call delivered",
            "transport_name": "test_voice",
            "transport_type": "voice",
            "helper_metadata": {}
        }
        inbound = Inbound.objects.create(**inbound_message)
        return str(inbound.id)

    def _replace_get_metric_client(self, session=None):
        return MetricsApiClient(
            auth_token=settings.METRICS_AUTH_TOKEN,
            api_url=settings.METRICS_URL,
            session=self.session)

    def _restore_get_metric_client(self, session=None):
        return MetricsApiClient(
            auth_token=settings.METRICS_AUTH_TOKEN,
            api_url=settings.METRICS_URL,
            session=session)

    def _replace_post_save_hooks_outbound(self):
        post_save.disconnect(fire_msg_action_if_new, sender=Outbound)

    def _replace_post_save_hooks_inbound(self):
        post_save.disconnect(fire_msg_action_if_new, sender=Inbound)

    def _restore_post_save_hooks_outbound(self):
        post_save.connect(fire_msg_action_if_new, sender=Outbound)

    def _restore_post_save_hooks_inbound(self):
        post_save.connect(fire_msg_action_if_new, sender=Inbound)

    def check_request(
            self, request, method, params=None, data=None, headers=None):
        self.assertEqual(request.method, method)
        if params is not None:
            url = urlparse.urlparse(request.url)
            qs = urlparse.parse_qsl(url.query)
            self.assertEqual(dict(qs), params)
        if headers is not None:
            for key, value in headers.items():
                self.assertEqual(request.headers[key], value)
        if data is None:
            self.assertEqual(request.body, None)
        else:
            self.assertEqual(json.loads(request.body), data)

    def _mount_session(self):
        response = [{
            'name': 'foo',
            'value': 9000,
            'aggregator': 'bar',
        }]
        adapter = RecordingAdapter(json.dumps(response).encode('utf-8'))
        self.session.mount(
            "http://metrics-url/metrics/", adapter)
        return adapter

    def setUp(self):
        super(AuthenticatedAPITestCase, self).setUp()
        self._replace_post_save_hooks_inbound
        tasks.get_metric_client = self._replace_get_metric_client
        self.adapter = self._mount_session()

        self.username = 'testuser'
        self.password = 'testpass'
        self.user = User.objects.create_user(self.username,
                                             'testuser@example.com',
                                             self.password)
        token = Token.objects.create(user=self.user)
        self.token = token.key
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + self.token)
        self.superuser = User.objects.create_superuser('testsu',
                                                       'su@example.com',
                                                       'dummypwd')
        sutoken = Token.objects.create(user=self.superuser)
        self.adminclient.credentials(
            HTTP_AUTHORIZATION='Token %s' % sutoken)

        self.handler = RecordingHandler()
        logger = logging.getLogger('go_http.test')
        logger.setLevel(logging.INFO)
        logger.addHandler(self.handler)

    def tearDown(self):
        self._restore_post_save_hooks_inbound()
        tasks.get_metric_client = self._restore_get_metric_client

    def check_logs(self, msg):
        if self.handler.logs is None:  # nothing to check
            return False
        if type(self.handler.logs) != list:
            [logs] = self.handler.logs
        else:
            logs = self.handler.logs
        for log in logs:
            logline = log.msg.replace("u'", "'")
            if logline == msg:
                return True
        return False


class TestVumiMessagesAPI(AuthenticatedAPITestCase):

    def test_create_outbound_data(self):
        post_outbound = {
            "to_addr": "+27123",
            "vumi_message_id": "075a32da-e1e4-4424-be46-1d09b71056fd",
            "content": "Say something",
            "delivered": False,
            "attempts": 0,
            "metadata": {}
        }
        response = self.client.post('/api/v1/outbound/',
                                    json.dumps(post_outbound),
                                    content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        d = Outbound.objects.last()
        self.assertIsNotNone(d.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(str(d.to_addr), "+27123")
        self.assertEqual(d.content, "Say something")
        self.assertEqual(d.delivered, False)
        self.assertEqual(d.attempts, 1)
        self.assertEqual(d.metadata, {})

    def test_create_outbound_data_simple(self):
        post_outbound = {
            "to_addr": "+27123",
            "delivered": "false",
            "metadata": {
                "voice_speech_url": "https://foo.com/file.mp3"
            }
        }
        response = self.client.post('/api/v1/outbound/',
                                    json.dumps(post_outbound),
                                    content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        d = Outbound.objects.last()
        self.assertIsNotNone(d.id)
        self.assertEqual(d.version, 1)
        self.assertEqual(str(d.to_addr), "+27123")
        self.assertEqual(d.delivered, False)
        self.assertEqual(d.attempts, 1)
        self.assertEqual(d.metadata, {
            "voice_speech_url": "https://foo.com/file.mp3"
        })

    def test_update_outbound_data(self):
        existing = self.make_outbound()
        patch_outbound = {
            "delivered": "true",
            "attempts": 2
        }
        response = self.client.patch('/api/v1/outbound/%s/' %
                                     existing,
                                     json.dumps(patch_outbound),
                                     content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        d = Outbound.objects.get(pk=existing)
        self.assertEqual(d.version, 1)
        self.assertEqual(str(d.to_addr), "+27123")
        self.assertEqual(d.delivered, True)
        self.assertEqual(d.attempts, 2)

    def test_delete_outbound_data(self):
        existing = self.make_outbound()
        response = self.client.delete('/api/v1/outbound/%s/' %
                                      existing,
                                      content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        d = Outbound.objects.filter(id=existing).count()
        self.assertEqual(d, 0)

    def test_create_inbound_data(self):
        existing_outbound = self.make_outbound()
        out = Outbound.objects.get(pk=existing_outbound)
        message_id = str(uuid.uuid4())
        post_inbound = {
            "message_id": message_id,
            "in_reply_to": out.vumi_message_id,
            "to_addr": "+27123",
            "from_addr": "020",
            "content": "Call delivered",
            "transport_name": "test_voice",
            "transport_type": "voice",
            "helper_metadata": {}
        }
        response = self.client.post('/api/v1/inbound/',
                                    json.dumps(post_inbound),
                                    content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        d = Inbound.objects.last()
        self.assertIsNotNone(d.id)
        self.assertEqual(d.message_id, message_id)
        self.assertEqual(d.to_addr, "+27123")
        self.assertEqual(d.from_addr, "020")
        self.assertEqual(d.content, "Call delivered")
        self.assertEqual(d.transport_name, "test_voice")
        self.assertEqual(d.transport_type, "voice")
        self.assertEqual(d.helper_metadata, {})

    def test_update_inbound_data(self):
        existing_outbound = self.make_outbound()
        out = Outbound.objects.get(pk=existing_outbound)
        existing = self.make_inbound(out.vumi_message_id)

        patch_inbound = {
            "content": "Opt out"
        }
        response = self.client.patch('/api/v1/inbound/%s/' %
                                     existing,
                                     json.dumps(patch_inbound),
                                     content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        d = Inbound.objects.get(pk=existing)
        self.assertEqual(d.to_addr, "+27123")
        self.assertEqual(d.from_addr, "020")
        self.assertEqual(d.content, "Opt out")
        self.assertEqual(d.transport_name, "test_voice")
        self.assertEqual(d.transport_type, "voice")
        self.assertEqual(d.helper_metadata, {})

    def test_delete_inbound_data(self):
        existing_outbound = self.make_outbound()
        out = Outbound.objects.get(pk=existing_outbound)
        existing = self.make_inbound(out.vumi_message_id)
        response = self.client.delete('/api/v1/inbound/%s/' %
                                      existing,
                                      content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        d = Inbound.objects.filter(id=existing).count()
        self.assertEqual(d, 0)

    def test_event_ack(self):
        existing = self.make_outbound()

        d = Outbound.objects.get(pk=existing)
        ack = {
            "message_type": "event",
            "event_id": "b04ec322fc1c4819bc3f28e6e0c69de6",
            "event_type": "ack",
            "user_message_id": d.vumi_message_id,
            "helper_metadata": {},
            "timestamp": "2015-10-28 16:19:37.485612",
            "sent_message_id": "external-id"
        }
        response = self.client.post('/api/v1/events',
                                    json.dumps(ack),
                                    content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        d = Outbound.objects.get(pk=existing)
        self.assertEqual(d.delivered, True)
        self.assertEqual(d.attempts, 1)
        self.assertEqual(d.metadata["ack_timestamp"],
                         "2015-10-28 16:19:37.485612")
        self.assertEquals(False, self.check_logs(
            "Message: 'Simple outbound message' sent to '+27123'"))

    def test_event_delivery_report(self):
        existing = self.make_outbound()
        d = Outbound.objects.get(pk=existing)
        dr = {
            "message_type": "event",
            "event_id": "b04ec322fc1c4819bc3f28e6e0c69de6",
            "event_type": "delivery_report",
            "user_message_id": d.vumi_message_id,
            "helper_metadata": {},
            "timestamp": "2015-10-28 16:20:37.485612",
            "sent_message_id": "external-id"
        }
        response = self.client.post('/api/v1/events',
                                    json.dumps(dr),
                                    content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        d = Outbound.objects.get(pk=existing)
        self.assertEqual(d.delivered, True)
        self.assertEqual(d.attempts, 1)
        self.assertEqual(d.metadata["delivery_timestamp"],
                         "2015-10-28 16:20:37.485612")
        self.assertEquals(False, self.check_logs(
            "Message: 'Simple outbound message' sent to '+27123'"))

    def test_event_nack_first(self):
        existing = self.make_outbound()
        d = Outbound.objects.get(pk=existing)
        post_save.connect(fire_msg_action_if_new, sender=Outbound)
        nack = {
            "message_type": "event",
            "event_id": "b04ec322fc1c4819bc3f28e6e0c69de6",
            "event_type": "nack",
            "nack_reason": "no answer",
            "user_message_id": d.vumi_message_id,
            "helper_metadata": {},
            "timestamp": "2015-10-28 16:20:37.485612",
            "sent_message_id": "external-id"
        }
        response = self.client.post('/api/v1/events',
                                    json.dumps(nack),
                                    content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        c = Outbound.objects.get(pk=existing)
        self.assertEqual(c.delivered, False)
        self.assertEqual(c.attempts, 2)
        self.assertEqual(c.metadata["nack_reason"],
                         "no answer")
        self.assertEquals(True, self.check_logs(
            "Message: 'Simple outbound message' sent to '+27123' "
            "[session_event: new]"))
        # TODO: Bring metrics back
        # self.assertEquals(
        #     True,
        #     self.check_logs("Metric: 'vumimessage.tries' [sum] -> 1"))

    def test_event_nack_last(self):
        # Be assured this is last message attempt
        outbound_message = {
            "to_addr": "+27123",
            "vumi_message_id": "08b34de7-c6da-4853-a74d-9458533ed169",
            "content": "Simple outbound message",
            "delivered": False,
            "attempts": 3,
            "metadata": {}
        }
        failed = Outbound.objects.create(**outbound_message)
        post_save.connect(fire_msg_action_if_new, sender=Outbound)
        nack = {
            "message_type": "event",
            "event_id": "b04ec322fc1c4819bc3f28e6e0c69de6",
            "event_type": "nack",
            "nack_reason": "no answer",
            "user_message_id": failed.vumi_message_id,
            "helper_metadata": {},
            "timestamp": "2015-10-28 16:20:37.485612",
            "sent_message_id": "external-id"
        }
        response = self.client.post('/api/v1/events',
                                    json.dumps(nack),
                                    content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        d = Outbound.objects.get(pk=failed.id)
        self.assertEqual(d.delivered, False)
        self.assertEqual(d.attempts, 3)  # not moved on as last attempt passed
        self.assertEqual(d.metadata["nack_reason"],
                         "no answer")
        self.assertEquals(False, self.check_logs(
            "Message: 'Simple outbound message' sent to '+27123'"
            "[session_event: new]"))
        # TODO: Bring metrics back
        # self.assertEquals(
        #     False,
        #     self.check_logs("Metric: 'vumimessage.tries' [sum] -> 1"))
        # self.assertEquals(
        #     True,
        #     self.check_logs("Metric: 'vumimessage.maxretries' [sum] -> 1"))


class TestJunebugMessagesAPI(AuthenticatedAPITestCase):
    def test_event_missing_fields(self):
        '''
        If there are missing fields in the request, and error response should
        be returned.
        '''
        response = self.client.post(
            '/api/v1/events/junebug', json.dumps({}),
            content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_event_no_message(self):
        '''
        If we cannot find the message for the event, and error response should
        be returned.
        '''
        ack = {
            "event_type": "submitted",
            "message_id": 'bad-message-id',
            "channel-id": "channel-uuid-1234",
            "timestamp": "2015-10-28 16:19:37.485612",
            "event_details": {},
        }
        response = self.client.post(
            '/api/v1/events/junebug', json.dumps(ack),
            content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_event_ack(self):
        '''A submitted event should update the message object accordingly.'''
        existing = self.make_outbound()

        d = Outbound.objects.get(pk=existing)
        ack = {
            "event_type": "submitted",
            "message_id": d.vumi_message_id,
            "channel-id": "channel-uuid-1234",
            "timestamp": "2015-10-28 16:19:37.485612",
            "event_details": {},
        }
        response = self.client.post(
            '/api/v1/events/junebug', json.dumps(ack),
            content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        d = Outbound.objects.get(pk=existing)
        self.assertEqual(d.delivered, True)
        self.assertEqual(d.attempts, 1)
        self.assertEqual(
            d.metadata["ack_timestamp"], "2015-10-28 16:19:37.485612")
        self.assertEquals(False, self.check_logs(
            "Message: 'Simple outbound message' sent to '+27123'"))

    def test_event_nack(self):
        '''
        A rejected event should retry and update the message object accordingly
        '''
        existing = self.make_outbound()
        d = Outbound.objects.get(pk=existing)
        post_save.connect(fire_msg_action_if_new, sender=Outbound)
        nack = {
            "event_type": "rejected",
            "message_id": d.vumi_message_id,
            "channel-id": "channel-uuid-1234",
            "timestamp": "2015-10-28 16:19:37.485612",
            "event_details": {"reason": "No answer"},
        }
        response = self.client.post(
            '/api/v1/events/junebug', json.dumps(nack),
            content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        c = Outbound.objects.get(pk=existing)
        self.assertEqual(c.delivered, False)
        self.assertEqual(c.attempts, 2)
        self.assertEqual(
            c.metadata["nack_reason"], {"reason": "No answer"})
        self.assertEquals(True, self.check_logs(
            "Message: 'Simple outbound message' sent to '+27123' "
            "[session_event: new]"))

    def test_event_delivery_succeeded(self):
        '''A successful delivery should update the message accordingly.'''
        existing = self.make_outbound()
        d = Outbound.objects.get(pk=existing)
        dr = {
            "event_type": "delivery_succeeded",
            "message_id": d.vumi_message_id,
            "channel-id": "channel-uuid-1234",
            "timestamp": "2015-10-28 16:19:37.485612",
            "event_details": {},
        }
        response = self.client.post(
            '/api/v1/events/junebug', json.dumps(dr),
            content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        d = Outbound.objects.get(pk=existing)
        self.assertEqual(d.delivered, True)
        self.assertEqual(d.attempts, 1)
        self.assertEqual(
            d.metadata["delivery_timestamp"], "2015-10-28 16:19:37.485612")
        self.assertEquals(False, self.check_logs(
            "Message: 'Simple outbound message' sent to '+27123'"))

    def test_event_delivery_failed(self):
        '''
        A failed delivery should retry and update the message accordingly.
        '''
        existing = self.make_outbound()
        d = Outbound.objects.get(pk=existing)
        dr = {
            "event_type": "delivery_failed",
            "message_id": d.vumi_message_id,
            "channel-id": "channel-uuid-1234",
            "timestamp": "2015-10-28 16:19:37.485612",
            "event_details": {},
        }
        response = self.client.post(
            '/api/v1/events/junebug', json.dumps(dr),
            content_type='application/json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        d = Outbound.objects.get(pk=existing)
        self.assertEqual(d.delivered, False)
        self.assertEqual(d.attempts, 2)
        self.assertEqual(
            d.metadata["delivery_failed_reason"], {})
        self.assertEquals(False, self.check_logs(
            "Message: 'Simple outbound message' sent to '+27123'"))


class TestMetricsAPI(AuthenticatedAPITestCase):

    def test_metrics_read(self):
        # Setup
        # Execute
        response = self.client.get('/api/metrics/',
                                   content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["metrics_available"], [
                'inbounds.created.sum',
                'vumimessage.tries.sum',
                'vumimessage.maxretries.sum'
            ]
        )

    @responses.activate
    def test_post_metrics(self):
        # Setup
        # deactivate Testsession for this test
        self.session = None
        responses.add(responses.POST,
                      "http://metrics-url/metrics/",
                      json={"foo": "bar"},
                      status=200, content_type='application/json')
        # Execute
        response = self.client.post('/api/metrics/',
                                    content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["scheduled_metrics_initiated"], True)


class TestMetrics(AuthenticatedAPITestCase):

    def test_direct_fire(self):
        # Setup
        adapter = self._mount_session()
        # Execute
        result = fire_metric.apply_async(kwargs={
            "metric_name": 'foo.last',
            "metric_value": 1,
            "session": self.session
        })
        # Check
        self.check_request(
            adapter.request, 'POST',
            data={"foo.last": 1.0}
        )
        self.assertEqual(result.get(),
                         "Fired metric <foo.last> with value <1.0>")

    def test_created_metrics(self):
        # Setup
        adapter = self._mount_session()
        # reconnect metric post_save hook
        post_save.connect(fire_metrics_if_new, sender=Inbound)
        # make outbound
        existing_outbound = self.make_outbound()
        out = Outbound.objects.get(pk=existing_outbound)

        # Execute
        self.make_inbound(out.vumi_message_id)

        # Check
        self.check_request(
            adapter.request, 'POST',
            data={"inbounds.created.sum": 1.0}
        )
        # remove post_save hooks to prevent teardown errors
        post_save.disconnect(fire_metrics_if_new, sender=Inbound)


class TestHealthcheckAPI(AuthenticatedAPITestCase):

    def test_healthcheck_read(self):
        # Setup
        # Execute
        response = self.client.get('/api/health/',
                                   content_type='application/json')
        # Check
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["up"], True)
        self.assertEqual(response.data["result"]["database"], "Accessible")


class TestUserCreation(AuthenticatedAPITestCase):

    def test_create_user_and_token(self):
        # Setup
        user_request = {"email": "test@example.org"}
        # Execute
        request = self.adminclient.post('/api/v1/user/token/', user_request)
        token = request.json().get('token', None)
        # Check
        self.assertIsNotNone(
            token, "Could not receive authentication token on post.")
        self.assertEqual(
            request.status_code, 201,
            "Status code on /api/v1/user/token/ was %s (should be 201)."
            % request.status_code)

    def test_create_user_and_token_fail_nonadmin(self):
        # Setup
        user_request = {"email": "test@example.org"}
        # Execute
        request = self.client.post('/api/v1/user/token/', user_request)
        error = request.json().get('detail', None)
        # Check
        self.assertIsNotNone(
            error, "Could not receive error on post.")
        self.assertEqual(
            error, "You do not have permission to perform this action.",
            "Error message was unexpected: %s."
            % error)

    def test_create_user_and_token_not_created(self):
        # Setup
        user_request = {"email": "test@example.org"}
        # Execute
        request = self.adminclient.post('/api/v1/user/token/', user_request)
        token = request.json().get('token', None)
        # And again, to get the same token
        request2 = self.adminclient.post('/api/v1/user/token/', user_request)
        token2 = request2.json().get('token', None)

        # Check
        self.assertEqual(
            token, token2,
            "Tokens are not equal, should be the same as not recreated.")

    def test_create_user_new_token_nonadmin(self):
        # Setup
        user_request = {"email": "test@example.org"}
        request = self.adminclient.post('/api/v1/user/token/', user_request)
        token = request.json().get('token', None)
        cleanclient = APIClient()
        cleanclient.credentials(HTTP_AUTHORIZATION='Token %s' % token)
        # Execute
        request = cleanclient.post('/api/v1/user/token/', user_request)
        error = request.json().get('detail', None)
        # Check
        # new user should not be admin
        self.assertIsNotNone(
            error, "Could not receive error on post.")
        self.assertEqual(
            error, "You do not have permission to perform this action.",
            "Error message was unexpected: %s."
            % error)


class TestFormatter(TestCase):

    @override_settings(
        VOICE_TO_ADDR_FORMATTER='message_sender.formatters.noop')
    def test_noop(self):
        cb = load_callable(settings.VOICE_TO_ADDR_FORMATTER)
        self.assertEqual(cb('12345'), '12345')

    @override_settings(
        VOICE_TO_ADDR_FORMATTER='message_sender.formatters.vas2nets_voice')
    def test_vas2nets_voice(self):
        cb = load_callable(settings.VOICE_TO_ADDR_FORMATTER)
        self.assertEqual(cb('+23456'), '9056')
        self.assertEqual(cb('23456'), '9056')

    @override_settings(
        VOICE_TO_ADDR_FORMATTER='message_sender.formatters.vas2nets_text')
    def test_vas2nets_text(self):
        cb = load_callable(settings.VOICE_TO_ADDR_FORMATTER)
        self.assertEqual(cb('+23456'), '23456')
        self.assertEqual(cb('23456'), '23456')


class TestFactory(TestCase):

    @override_settings(MESSAGE_BACKEND_TEXT='junebug',
                       JUNEBUG_API_URL_TEXT='http://example.com/',
                       JUNEBUG_API_AUTH_TEXT=('username', 'password'))
    def test_create_junebug_text(self):
        message_sender = MessageClientFactory.create('text')
        self.assertTrue(isinstance(message_sender, JunebugApiSender))
        self.assertEqual(message_sender.api_url, 'http://example.com/')
        self.assertEqual(message_sender.auth, ('username', 'password'))

    @override_settings(MESSAGE_BACKEND_VOICE='junebug',
                       JUNEBUG_API_URL_VOICE='http://example.com/voice',
                       JUNEBUG_API_AUTH_VOICE=('username', 'password'))
    def test_create_junebug_voice(self):
        message_sender = MessageClientFactory.create('voice')
        self.assertTrue(isinstance(message_sender, JunebugApiSender))
        self.assertEqual(message_sender.api_url, 'http://example.com/voice')
        self.assertEqual(message_sender.auth, ('username', 'password'))

    @override_settings(MESSAGE_BACKEND_TEXT='vumi',
                       VUMI_CONVERSATION_KEY_TEXT='conv-key',
                       VUMI_ACCOUNT_KEY_TEXT='account-key',
                       VUMI_ACCOUNT_TOKEN_TEXT='account-token',
                       VUMI_API_URL_TEXT='http://example.com/')
    def test_create_vumi_text(self):
        message_sender = MessageClientFactory.create('text')
        self.assertTrue(isinstance(message_sender, HttpApiSender))
        self.assertEqual(
            message_sender.api_url, 'http://example.com/')
        self.assertEqual(message_sender.account_key, 'account-key')
        self.assertEqual(message_sender.conversation_key, 'conv-key')
        self.assertEqual(message_sender.conversation_token, 'account-token')

    @override_settings(MESSAGE_BACKEND_VOICE='vumi',
                       VUMI_CONVERSATION_KEY_VOICE='conv-key',
                       VUMI_ACCOUNT_KEY_VOICE='account-key',
                       VUMI_ACCOUNT_TOKEN_VOICE='account-token',
                       VUMI_API_URL_VOICE='http://example.com/')
    def test_create_vumi_voice(self):
        message_sender = MessageClientFactory.create('voice')
        self.assertTrue(isinstance(message_sender, HttpApiSender))
	self.assertEqual(
            message_sender.api_url, 'http://example.com/')
        self.assertEqual(message_sender.account_key, 'account-key')
        self.assertEqual(message_sender.conversation_key, 'conv-key')
        self.assertEqual(message_sender.conversation_token, 'account-token')

    @override_settings(MESSAGE_BACKEND_VOICE='unknown')
    def test_create_unknown(self):
        '''
        The message client factory should raise an exception if an unknown
        message type is specified.
        '''
        self.assertRaises(
            FactoryException, MessageClientFactory.create, 'voice')

    @override_settings(MESSAGE_BACKEND_VOICE=None)
    def test_create_no_backend_type_specified(self):
        '''
        If no message backend is specified, an error should be raised when
        getting the message client.
        '''
        self.assertRaises(
            FactoryException, MessageClientFactory.create, 'voice')


class TestJunebugAPISender(TestCase):
    @override_settings(MESSAGE_BACKEND_TEXT='junebug',
                       JUNEBUG_API_URL_TEXT='http://example.com/',
                       JUNEBUG_API_FROM_TEXT='+4321')
    @responses.activate
    def test_send_text(self):
        '''
        Using the send_text function should send a request to Junebug with the
        correct JSON data.
        '''
        responses.add(
            responses.POST, "http://example.com/",
            json={"result": {"message_id": "message-uuid"}}, status=200,
            content_type='application/json')

        message_sender = MessageClientFactory.create('text')
        res = message_sender.send_text('+1234', 'Test', session_event='resume')

        self.assertEqual(res['message_id'], 'message-uuid')

        [r] = responses.calls
        r = json.loads(r.request.body)
        self.assertEqual(r['to'], '+1234')
        self.assertEqual(r['from'], '+4321')
        self.assertEqual(r['content'], 'Test')
        self.assertEqual(r['channel_data']['session_event'], 'resume')

    @override_settings(MESSAGE_BACKEND_VOICE='junebug',
                       JUNEBUG_API_URL_VOICE='http://example.com/',
                       JUNEBUG_API_FROM_VOICE='+4321')
    @responses.activate
    def test_send_voice(self):
        '''
        Using the send_voice function should send a request to Junebug with the
        correct JSON data.
        '''
        responses.add(
            responses.POST, "http://example.com/",
            json={"result": {"message_id": "message-uuid"}}, status=200,
            content_type='application/json')

        message_sender = MessageClientFactory.create('voice')
        res = message_sender.send_voice(
            '+1234', 'Test', speech_url='http://test.mp3', wait_for='#',
            session_event='resume')

        self.assertEqual(res['message_id'], 'message-uuid')

        [r] = responses.calls
        r = json.loads(r.request.body)
        self.assertEqual(r['to'], '+1234')
        self.assertEqual(r['from'], '+4321')
        self.assertEqual(r['content'], 'Test')
        self.assertEqual(r['channel_data']['session_event'], 'resume')
        self.assertEqual(
            r['channel_data']['voice']['speech_url'], 'http://test.mp3')
        self.assertEqual(r['channel_data']['voice']['wait_for'], '#')

    @override_settings(MESSAGE_BACKEND_VOICE='junebug')
    def test_fire_metric(self):
        '''
        Using the fire_metric function should result in an exception being
        raised, since Junebug doesn't support metrics sending.
        '''
        message_sender = MessageClientFactory.create('voice')
        self.assertRaises(
            JunebugApiSenderException, message_sender.fire_metric, 'foo.bar',
            3.0, agg='sum')
