import base64
import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.utils.module_loading import import_string

from pyfcm import FCMNotification
from pyfcm.errors import AuthenticationError, FCMServerError, InvalidDataError, InternalPackageError

import redis

from .monitoring import FCM_API, monitor_duration
from .util import utc_timestamp

logger = logging.getLogger('drop_service.notify')


def get_notificators():
    """Return list of configured notificator instances."""
    notificators = []
    for class_path in settings.PUSH_NOTIFICATORS:
        cls = import_string(class_path)
        notificators += cls(),
    return notificators


class FCM:
    """
    Publish drops through FCM topic messages. The topic is the drop ID.
    """
    SERVICE = 'fcm'
    _logger = logger.getChild('fcm')

    def __init__(self, fcm_notification=None, executor=None):
        if not fcm_notification:
            fcm_notification = FCMNotification(api_key=settings.FCM_API_KEY, proxy_dict=settings.FCM_PROXY)
        self._executor = executor or ThreadPoolExecutor()
        self._push = fcm_notification

    def notify(self, drop):
        self._executor.submit(self._notify, drop)

    def _notify(self, drop):
        data = {
            'drop-id': drop.drop_id,
        #    'message': base64.b64encode(drop.message).decode(),
        }
        # Alphabet of topics: [a-zA-Z0-9-_.~%]
        # Alphabet of drop IDs: [a-zA-Z0-9-_]
        with monitor_duration(FCM_API, exception='None') as monitor_labels:
            try:
                # The response contains no useful data for topic messages
                # Downstream messages on the other hand include success/failure counts
                self._push.notify_topic_subscribers(topic_name=drop.drop_id, data_message=data)
            except (AuthenticationError, FCMServerError, InvalidDataError, InternalPackageError) as exc:
                monitor_labels['exception'] = type(exc).__name__
                self._logger.exception('notify_topic_subscribers API exception')


class Redis:
    def __init__(self):
        self._redis = redis.StrictRedis(host=settings.REDIS_HOST, port=settings.REDIS_PORT)
        self._prefix = settings.REDIS_PREFIX

    def notify(self, drop):
        headers = [
            'X-Qabel-Latest: ' + str(utc_timestamp(drop.created_at)),
            'Last-Modified: ' + drop.created_at.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        ]
        data = []
        data.append('\n'.join(headers).encode())
        data.append(b'\n\n')
        data.append(drop.message)
        self._redis.publish(self._prefix + drop.drop_id, b''.join(data))
