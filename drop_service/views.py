import datetime
import json
import logging
import uuid
from email.utils import formatdate
from time import mktime

import dateparser

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotModified

from rest_framework import status

from . import monitoring
from .models import Drop
from .notify import get_notificators
from .util import CsrfExemptView, check_drop_id, set_last_modified, utc_timestamp

logger = logging.getLogger(__name__)


def error(msg, status=status.HTTP_400_BAD_REQUEST):
    return HttpResponse(json.dumps({'error': msg}), status=status)


class DropView(CsrfExemptView):
    http_method_names = ['get', 'head', 'post']
    notificators = get_notificators()

    def _get_drops(self, drop_id):
        """Return (response, drops) for a request. response are errors and no-content/not-modified, otherwise None."""
        if not check_drop_id(drop_id):
            return error('Invalid drop id'), None

        drops = Drop.objects.filter(drop_id=drop_id)

        try:
            have_since, since = self.get_if_modified_since()
        except ValueError as value_error:
            logger.warning('Could not parse modified-since header pack: %s', value_error)
            have_since, since = False, None

        if have_since:
            drops = drops.filter(created_at__gt=since)


        if not drops.exists():
            if have_since:
                return HttpResponseNotModified(), None
            else:
                return HttpResponse(status=status.HTTP_204_NO_CONTENT), None

        return None, list(drops)

    def get(self, request, drop_id):
        response, drops = self._get_drops(drop_id)
        if response:
            return response

        monitoring.DROP_SENT.inc(len(drops))
        boundary = str(uuid.uuid4())
        content_type = 'multipart/mixed; boundary="{boundary}"'.format(boundary=boundary)
        body = self.generate_body(drops, boundary)
        response = HttpResponse(body, content_type=content_type)
        if drops:
            self.set_latest(response, drops[-1])
        return response

    def head(self, request, drop_id):
        response, _ = self._get_drops(drop_id)
        return response or HttpResponse()

    def post(self, request, drop_id):
        if not check_drop_id(drop_id):
            return error('Invalid drop id')

        authorization_header = request.META.get('HTTP_AUTHORIZATION')
        if authorization_header != 'Client Qabel':
            return error('Bad authorization')

        message = request.body
        if message == b'' or message is None:
            return error('No message provided')
        if len(message) > settings.MESSAGE_SIZE_LIMIT:
            return error('Message too large', status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        drop = Drop.objects.create(message=message, drop_id=drop_id)
        self.notify(drop)
        monitoring.DROP_RECEIVED.inc()
        return HttpResponse()

    def get_if_modified_since(self):
        coarse_since = self.request.META.get('HTTP_IF_MODIFIED_SINCE')
        finest_since = self.request.META.get('HTTP_X_QABEL_NEW_SINCE')
        if coarse_since and finest_since:
            raise ValueError('Specify only one of X-Qabel-New-Since, If-Modified-Since')
        if coarse_since:
            since = dateparser.parse(coarse_since)
            if not since:
                raise ValueError('Unable to parse If-Modified-Since')
            return True, since
        elif finest_since:
            return True, datetime.datetime.fromtimestamp(float(finest_since), datetime.timezone.utc)
        else:
            return False, None

    def set_latest(self, response, latest_drop):
        set_last_modified(response, latest_drop.created_at)
        timestamp = utc_timestamp(latest_drop.created_at)
        response['X-Qabel-Latest'] = str(timestamp)

    @staticmethod
    def generate_body(drops, boundary):
        boundary = boundary.encode()
        for drop in drops:
            date = formatdate(mktime(drop.created_at.timetuple()), localtime=True, usegmt=True).encode()
            yield (b'--' + boundary + b'\r\n')
            yield (b'Content-Type: application/octet-stream\r\n')
            yield (b'Date: ' + date + b'\r\n\r\n')
            yield (bytes(drop.message) + b'\r\n')
        yield (b'--' + boundary + b'--\r\n')

    def notify(self, drop):
        for notificator in self.notificators:
            notificator.notify(drop)
