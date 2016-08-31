# coding: utf-8

import logging
import requests
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from django.utils.translation import ugettext as _
from raven import Client
from plp.utils.edx_enrollment import EDXEnrollment, EDXNotAvailable, EDXCommunicationError, EDXEnrollmentError
from plp.models import CourseSession
from plp_extension.apps.course_extension.models import CourseExtendedParameters
from .models import EducationalModuleProgress, EducationalModuleRating

RAVEN_CONFIG = getattr(settings, 'RAVEN_CONFIG', {})
client = None

if RAVEN_CONFIG:
    client = Client(RAVEN_CONFIG.get('dsn'))

REQUEST_TIMEOUT = 10


class EDXTimeoutError(EDXEnrollmentError):
    pass


class EDXEnrollmentExtension(EDXEnrollment):
    """
    расширение класса EDXEnrollment с обработкой таймаута
    """
    def request(self, path, method='GET', **kwargs):
        url = '%s%s' % (self.base_url, path)

        headers = kwargs.setdefault('headers', {})

        if self.access_token:
            headers["Authorization"] = "Bearer %s" % self.access_token
        else:
            headers["X-Edx-Api-Key"] = settings.EDX_API_KEY
        if method == 'POST':
            headers['Content-Type'] = 'application/json'

        kwargs_copy = kwargs.copy()
        kwargs_copy.pop('headers', None)
        error_data = {
            'path': path,
            'method': method,
            'data': kwargs_copy,
        }
        try:
            logging.debug("EDXEnrollment.request %s %s %s", method, url, kwargs)
            r = self.session.request(method=method, url=url, **kwargs)
        except requests.exceptions.Timeout:
            if client:
                client.captureMessage('EDX connection timeout', extra=error_data)
            logging.error('Edx connection timeout error: %s' % error_data)
            raise EDXTimeoutError('')
        except IOError as exc:
            error_data['exception'] = str(exc)
            if client:
                client.captureMessage('EDXNotAvailable', extra=error_data)
            logging.error('EDXNotAvailable error: %s' % error_data)
            raise EDXNotAvailable("Error: {}".format(exc))

        logging.debug("EDXEnrollment.request response=%s %s", r.status_code, r.content)

        error_data.update({'status_code': r.status_code, 'content': r.content})
        if 500 <= r.status_code:
            if client:
                client.captureMessage('EDXNotAvailable', extra=error_data)
            logging.error('EDXNotAvailable error: %s' % error_data)
            raise EDXNotAvailable("Invalid EDX http response: {} {}".format(r.status_code, r.content))

        if r.status_code != 200:
            if client:
                client.captureMessage('EDXCommunicationError', extra=error_data)
            logging.error('EDXCommunicationError error: %s' % error_data)
            raise EDXCommunicationError("Invalid EDX http response: {} {}".format(r.status_code, r.content))

        return r

    def get_courses_progress(self, user_id, course_ids, timeout=REQUEST_TIMEOUT):
        query = 'user_id={}&course_id={}'.format(
            user_id,
            ','.join(course_ids)
        )
        return self.request(
            method='GET',
            path='/api/extended/edmoduleprogress?{}'.format(query),
            timeout=timeout
        )


def update_module_enrollment_progress(enrollment):
    """
    обновление прогресса из edx по сессиям курсов, входящих в модуль, на который записан пользователь
    """
    module = enrollment.module
    sessions = CourseSession.objects.filter(course__in=module.courses.all())
    course_ids = [s.get_absolute_slug_v1() for s in sessions if s.course_status().get('code') == 'started']
    try:
        data = EDXEnrollmentExtension().get_courses_progress(enrollment.user.username, course_ids).json()
        now = timezone.now().strftime('%H:%M:%S %Y-%m-%d')
        for k, v in data.iteritems():
            v['updated_at'] = now
        try:
            progress = EducationalModuleProgress.objects.get(enrollment=enrollment)
            p = progress.progress or {}
            p.update(data)
            progress.progress = p
            progress.save()
        except EducationalModuleProgress.DoesNotExist:
            EducationalModuleProgress.objects.create(enrollment=enrollment, progress=data)
    except EDXEnrollmentError:
        pass


def get_feedback_list(module):
    filter_dict = {
        'content_type': ContentType.objects.get_for_model(module),
        'object_id': module.id,
        'status': 'published',
        'declined': False,
    }
    rating_list = EducationalModuleRating.objects.filter(**filter_dict).order_by('-updated_at')[:2]
    return rating_list


def get_status_dict(session):
    months = {
        1: _(u'января'),
        2: _(u'февраля'),
        3: _(u'марта'),
        4: _(u'апреля'),
        5: _(u'мая'),
        6: _(u'июня'),
        7: _(u'июля'),
        8: _(u'августа'),
        9: _(u'сенятбря'),
        10: _(u'октября'),
        11: _(u'ноября'),
        12: _(u'декабря'),
    }
    if session:
        status = session.course_status()
        d = {'status': status['code']}
        if status['code'] == 'scheduled':
            starts = timezone.localtime(session.datetime_starts).date()
            d['days_before_start'] = (starts - timezone.now().date()).days
            d['date'] = session.datetime_starts.strftime('%d.%m.%Y')
            day, month = starts.day, months.get(starts.month)
            d['date_words'] = _(u'начало {day} {month}').format(day=day, month=month)
        elif status['code'] == 'started':
            ends = timezone.localtime(session.datetime_end_enroll)
            d['date'] = ends.strftime('%d.%m.%Y')
            day, month = ends.day, months.get(ends.month)
            d['date_words'] = _(u'запись до {day} {month}').format(day=day, month=month)
        return d
    else:
        return {'status': ''}


def choose_closest_session(c):
    sessions = c.course_sessions.all()
    if sessions:
        sessions = filter(lambda x: x.datetime_end_enroll and x.datetime_end_enroll > timezone.now()
                                and x.datetime_starts, sessions)
        sessions = sorted(sessions, key=lambda x: x.datetime_end_enroll)
        if sessions:
            return sessions[0]
    return None


def course_set_attrs(instance):
    """
    копируем атрибуты связанного CourseExtendedParams, добавляем методы
    """
    def _get_next_session(self):
        return choose_closest_session(self)

    def _get_course_status_params(self):
        return get_status_dict(self.get_next_session())

    new_methods = {
        'get_next_session': _get_next_session,
        'course_status_params': _get_course_status_params,
    }

    for name, method in new_methods.iteritems():
        setattr(instance, name, method)

    try:
        ext = instance.extended_params
    except CourseExtendedParameters.DoesNotExist:
        ext = None
    for field in CourseExtendedParameters._meta.fields:
        if not field.auto_created and field.editable:
            if ext:
                setattr(instance, field.name, getattr(ext, field.name))
            else:
                setattr(instance, field.name, None)

    return instance