
import json
import uuid
from email.utils import formatdate
from time import mktime

import dateparser

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotModified
from django.views.generic import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from rest_framework import status

from . import monitoring
from .models import Drop
from .util import check_drop_id, set_last_modified


def error(msg, status=status.HTTP_400_BAD_REQUEST):
    return HttpResponse(json.dumps({'error': msg}), status=status)


class DropView(View):
    http_method_names = ['get', 'head', 'post']

    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def _get_drops(self, drop_id):
        """Return (response, drops) for a request. response are errors and no-content/not-modified, otherwise None."""
        if not check_drop_id(drop_id):
            return error('Invalid drop id'), None

        drops = Drop.objects.filter(drop_id=drop_id)
        if not drops:
            return HttpResponse(status=status.HTTP_204_NO_CONTENT), None

        have_since, since = self.get_if_modified_since()
        if have_since:
            drops = drops.filter(created_at__gt=since)
            if not drops:
                return HttpResponseNotModified(), None

        return None, drops

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
            set_last_modified(response, drops.latest().created_at)
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
        Drop.objects.create(message=message, drop_id=drop_id)
        monitoring.DROP_RECEIVED.inc()
        return HttpResponse()

    def get_if_modified_since(self):
        since = self.request.META.get('HTTP_IF_MODIFIED_SINCE')
        if since:
            return True, dateparser.parse(since)
        else:
            return False, None

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
