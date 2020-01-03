import math
import os.path
import uuid
from collections import defaultdict

import magic
import requests

from datetime import date

from bs4 import BeautifulSoup

from functools import lru_cache

from urllib.parse import urlparse, urlencode

from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import AnonymousUser
from django.utils.encoding import smart_text
from django.urls import reverse
from django.core.files.base import ContentFile
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from django.db.models import Q, signals, F
from django.db.models.functions import Greatest
from django.contrib.contenttypes.fields import GenericForeignKey
from django.db.models import ObjectDoesNotExist
from django.utils.functional import cached_property
from django.conf import settings

from pybb.compat import update_fields, AUTH_USER_MODEL, queryset
from pybb.util import unescape, get_model_string, tznow
from pybb.base import ModelBase, ManagerBase, QuerySetBase
from pybb.models.mixins import ParentForumQuerysetMixin, ParentForumManagerMixin, ParentForumBase
from pybb.subscription import notify_topic_subscribers
from pybb import defaults
from pybb.fields import ContentTypeRestrictedFileField
from pybb.tasks import generate_markup, sync_cover
from pybb.processors import markup

from autoslug import AutoSlugField

if 'sorl.thumbnail' in settings.INSTALLED_APPS:
    from sorl.thumbnail import ImageField
else:
    from django.db.models import ImageField


class ModeratorManager(ManagerBase):
    def contribute_to_class(self, cls, name):
        signals.post_delete.connect(self.post_delete, sender=cls)
        return super(ModeratorManager, self).contribute_to_class(cls, name)

    def post_delete(self, instance, **kwargs):
        from pybb.proxies import UserObjectPermission

        UserObjectPermission.objects.get_for_object(instance.user, instance.forum).delete()

        (instance.user.user_permissions.through.objects.filter(permission__codename__in=defaults.PYBB_USER_PERMISSIONS,
                                                               user=instance.user)
         .delete())


class BaseModerator(ModelBase):
    forum = models.ForeignKey(get_model_string('Forum'), on_delete=models.CASCADE)
    user = models.ForeignKey(AUTH_USER_MODEL, on_delete=models.CASCADE)

    objects = ModeratorManager()

    class Meta:
        app_label = 'pybb'
        verbose_name = _('Moderator')
        verbose_name_plural = _('Moderators')
        abstract = True

    def __str__(self):
        return _('Moderator %(user)s of %(forum)s') % {
            'user': self.user,
            'forum': self.forum
        }

    @property
    def permissions(self):
        from pybb.proxies import UserObjectPermission

        permissions = list(self.user.user_permissions.all())
        permissions += [obj_perm.permission
                        for obj_perm in (UserObjectPermission.objects.get_for_object(user=self.user,
                                                                                     obj=self.forum)
                                         .select_related('permission'))]

        return permissions


class ForumQuerySet(ParentForumQuerysetMixin, QuerySetBase):
    def filter_by_user(self, user, hidden=True):
        if user.is_staff or user.is_superuser:
            return self

        if user.is_authenticated:
            return self.filter(staff=False)

        if hidden:
            return self.filter(hidden=False, staff=False)

        return self.filter(staff=False)


@queryset
class ForumManager(ParentForumManagerMixin, ManagerBase):
    def contribute_to_class(self, cls, name):
        signals.post_save.connect(self.post_save, sender=cls)
        signals.post_delete.connect(self.post_delete, sender=cls)
        return super(ForumManager, self).contribute_to_class(cls, name)

    def post_save(self, instance, **kwargs):
        if kwargs.get('created', False) and instance.forum_id:
            instance.forum.compute()

    def post_delete(self, instance, **kwargs):
        if instance.forum_id:
            instance.forum.compute()

    def get_queryset(self):
        return ForumQuerySet(self.model)

    def filter_by_user(self, *args, **kwargs):
        return self.get_queryset().filter_by_user(*args, **kwargs)


@lru_cache()
def get_moderator_ids_by_forum():
    from pybb.models import Moderator

    result = defaultdict(list)
    for moderator in Moderator.objects.all():
        result[moderator.forum_id].append(moderator.user_id)

    return result


class BaseForum(ParentForumBase):
    forum = models.ForeignKey('Forum', related_name='forums',
                              verbose_name=_('Parent'), null=True, blank=True, on_delete=models.PROTECT)
    name = models.CharField(_('Name'), max_length=80)
    slug = AutoSlugField(populate_from='name', max_length=80)
    position = models.IntegerField(_('Position'), blank=True, default=0, db_index=True)
    description = models.TextField(_('Description'), blank=True)
    moderators = models.ManyToManyField(AUTH_USER_MODEL, blank=True, verbose_name=_('Moderators'), through=get_model_string('Moderator'))
    updated = models.DateTimeField(_('Updated'), blank=True, null=True)
    post_count = models.IntegerField(_('Post count'), blank=True, default=0, db_index=True)
    member_count = models.IntegerField(_('Member count'), blank=True, default=0, db_index=True)
    topic_count = models.IntegerField(_('Topic count'), blank=True, default=0, db_index=True)
    forum_count = models.PositiveIntegerField(default=0, db_index=True)
    readed_by = models.ManyToManyField(AUTH_USER_MODEL, through=get_model_string('ForumReadTracker'), related_name='readed_forums')
    headline = models.TextField(_('Headline'), blank=True, null=True)
    hidden = models.BooleanField(_('Hidden'), blank=False, null=False,
                                 default=False, db_index=True)
    staff = models.BooleanField(_('Staff only'), blank=False,
                                null=False, default=False,
                                db_index=True)
    empty = models.BooleanField(_('Empty'),
                                default=False,
                                db_index=True,
                                help_text=_('If the forum is empty, you can\'t post into it'))

    last_topic = models.ForeignKey(get_model_string('Topic'),
                                   null=True,
                                   blank=True,
                                   related_name='last_forums',
                                   on_delete=models.SET_NULL)

    last_post = models.ForeignKey(get_model_string('Post'),
                                  null=True,
                                  blank=True,
                                  on_delete=models.SET_NULL)

    objects = ForumManager()

    class Meta(object):
        verbose_name = _('Forum')
        verbose_name_plural = _('Forums')
        ordering = ['position']
        permissions = (
            ('can_unstick_topic', _('Can unstick topic')),
            ('can_stick_topic', _('Can stick topic')),
            ('can_open_topic', _('Can open topic')),
            ('can_close_topic', _('Can close topic')),
            ('can_merge_topic', _('Can merge topic')),
            ('can_delete_topic', _('Can delete topic')),
            ('can_change_topic', _('Can edit topic')),
            ('can_move_topic', _('Can move topic')),
            ('can_move_post', _('Can move post')),
            ('can_change_poll', _('Can edit a poll')),
            ('can_publish_announce', _('Can publish an announce')),
            ('can_change_post', _('Can edit post')),
            ('can_delete_post', _('Can delete post')),
            ('can_see_user_ip', _('Can see IP Address')),
            ('can_change_attachment', _('Can moderate attachments')),
        )
        app_label = 'pybb'
        abstract = True

    def compute(self, commit=True):
        forum_count = self.forums.count()

        from pybb.models import Topic, Post

        member_count_aggregate = (Post.objects
                                  .visible(join=True)
                                  .filter(topic__forum_ids__contains=[self.id])
                                  .aggregate(models.Count('user_id', distinct=True)))

        res = Topic.objects.visible().filter(forum_id=self.pk).aggregate(post_count=models.Sum('post_count'))

        post_count = res['post_count'] or 0

        topic_count = Topic.objects.filter(forum_id=self.pk).visible().count() or 0

        res = self.__class__.objects.filter(forum_id=self.pk).aggregate(post_count=models.Sum('post_count'),
                                                                        topic_count=models.Sum('topic_count'),
                                                                        forum_count=models.Sum('forum_count'))

        self.post_count = post_count + (res['post_count'] or 0)

        self.member_count = member_count_aggregate['user_id__count']

        self.topic_count = topic_count + (res['topic_count'] or 0)

        self.forum_count = forum_count + (res['forum_count'] or 0)

        if commit:
            self.save(update_fields=['post_count', 'member_count', 'topic_count', 'forum_count'])

        if self.forum_id and self.forum_id != self.pk:
            self.forum.compute(commit=commit)

    def is_moderated_by(self, user, permission=None):
        if user.is_superuser or user.is_staff:
            return True

        if (user.is_authenticated and
                user.pk in get_moderator_ids_by_forum()[self.pk]):

            if permission:
                return user.has_perm(permission, self)

            return True

        return False

    def is_hidden(self):
        return self.hidden or (self.forum_id and self.forum.is_hidden())

    def is_accessible_by(self, user, hidden=True):
        if self.forum_id and not self.forum.is_accessible_by(user, hidden=hidden):
            return False

        if self.staff and not user.is_staff:
            return False

        if hidden and self.is_hidden() and not user.is_authenticated:
            return False

        return True

    def __str__(self):
        return self.name

    def update_counters(self, commit=True):
        last_post = self.get_last_post()

        if last_post:
            self.updated = last_post.created

            self.last_post = last_post
            self.last_topic = last_post.topic

        self.compute(commit=commit)

        if commit:
            self.save(update_fields=['updated', 'last_post', 'last_topic'])

    def get_absolute_url(self):
        return reverse('pybb:forum_detail', kwargs={'slug': self.slug})

    @property
    def posts(self):
        from pybb.models import Post

        return Post.objects.filter(topic__forum=self).visible().select_related()

    def get_last_post(self):
        from pybb.models import Post

        post = None

        try:
            topic = self.topics.visible().order_by('-updated')[0]
            post = topic.last_post

            if post:
                post.topic = topic
        except (IndexError, Post.DoesNotExist):
            pass

        if post and post.topic.poll_id:
            try:
                other = self.topics.visible().filter(poll__isnull=True).order_by('-updated')[0].last_post
            except (IndexError, Post.DoesNotExist):
                pass
            else:
                if post:
                    if other.created > post.created:
                        post = other
                else:
                    post = other

        return post

    def get_last_topic(self):
        topic = None

        try:
            topic = self.topics.order_by('-updated').visible().select_related()[0]
        except IndexError:
            return None

        try:
            forum_topic = self.forums.order_by('-updated').filter(last_topic__isnull=False)[0].last_topic

            if topic:
                if forum_topic.updated > topic.updated:
                    topic = forum_topic
            else:
                topic = forum_topic
        except IndexError:
            pass

        return topic

    @classmethod
    def watch_forum(cls, old_attr={}, new_attr={}, instance=None, sender=None, **kw):
        from pybb.models import Forum

        if (old_attr.get('forum_id', None) and
                old_attr.get('forum_id') != new_attr.get('forum_id')):
            instance.save(_signal=False)

            try:
                Forum.objects.get(pk=old_attr.get('forum_id')).update_counters()
            except ObjectDoesNotExist:
                pass


class BaseTopicRedirection(ModelBase):
    TYPE_PERMANENT_REDIRECT = 1
    TYPE_NO_REDIRECT = 2
    TYPE_EXPIRING_REDIRECT = 3

    TYPE_CHOICES = (
        (TYPE_PERMANENT_REDIRECT, _('Permanent redirect')),
        (TYPE_NO_REDIRECT, _('Leave no redirect')),
        (TYPE_EXPIRING_REDIRECT, _('Expiring redirect')),
    )

    from_topic = models.OneToOneField(get_model_string('Topic'),
                                      related_name='redirection', on_delete=models.CASCADE)
    to_topic = models.ForeignKey(get_model_string('Topic'),
                                 related_name='redirections', on_delete=models.CASCADE)
    created = models.DateTimeField(_('Created'), auto_now_add=True)
    type = models.PositiveSmallIntegerField(choices=TYPE_CHOICES,
                                            default=TYPE_PERMANENT_REDIRECT,
                                            db_index=True)
    expired = models.DateField(_('Expired'), null=True)

    class Meta(object):
        verbose_name = _('Redirection')
        verbose_name_plural = _('Redirections')
        app_label = 'pybb'
        abstract = True
        ordering = ['-created']

    def is_expired(self):
        return self.expired < date.today()

    def is_type_permanent(self):
        return self.type == self.TYPE_PERMANENT_REDIRECT

    def is_type_no(self):
        return self.type == self.TYPE_NO_REDIRECT

    def is_type_expiring(self):
        return self.type == self.TYPE_EXPIRING_REDIRECT


class TopicQuerySetMixin(object):
    def filter_by_user(self, user, forum=None, join=True):
        if forum is not None:
            if not forum.is_moderated_by(user):
                if user.is_authenticated:
                    return (self.filter(Q(user=user) | ~Q(on_moderation=BaseTopic.MODERATION_IS_IN_MODERATION))
                            .exclude(deleted=True))
            else:
                return self

            return self.exclude(on_moderation=BaseTopic.MODERATION_IS_IN_MODERATION).exclude(deleted=True)

        if user.is_staff or user.is_superuser:
            return self

        qs = self
        if user.is_authenticated:
            if join:
                qs = qs.filter(forum__staff=False)
            return (qs.filter(Q(user=user) | ~Q(on_moderation=BaseTopic.MODERATION_IS_IN_MODERATION))
                    .exclude(deleted=True))

        if join:
            qs = qs.filter(forum__hidden=False, forum__staff=False)
        return (qs.exclude(on_moderation=BaseTopic.MODERATION_IS_IN_MODERATION)
                .exclude(deleted=True))

    def visible(self):
        return self.filter(deleted=False,
                           redirect=False).exclude(on_moderation=BaseTopic.MODERATION_IS_IN_MODERATION)


class TopicQuerySet(ParentForumQuerysetMixin, TopicQuerySetMixin, QuerySetBase):
    pass


@queryset
class TopicManager(ParentForumManagerMixin, ManagerBase):
    def get_queryset(self):
        return TopicQuerySet(self.model)

    def filter_by_user(self, user, forum=None):
        return self.get_queryset().filter_by_user(user, forum=forum)

    def visible(self):
        return self.get_queryset().visible()


class SubscriptionQuerySet(QuerySetBase):
    def visible(self):
        return self.filter(topic__deleted=False,
                           topic__redirect=False).exclude(topic__on_moderation=BaseTopic.MODERATION_IS_IN_MODERATION)


@queryset
class SubscriptionManager(ManagerBase):
    def get_queryset(self):
        return SubscriptionQuerySet(self.model)

    def visible(self):
        return self.get_queryset().visible()


class BaseSubscription(ModelBase):
    TYPE_NO_ALERT = 0
    TYPE_INSTANT_ALERT = 1
    TYPE_DAILY_ALERT = 2
    TYPE_CHOICES = (
        (TYPE_NO_ALERT, _('No notifications')),
        (TYPE_INSTANT_ALERT, _('Send alerts by email')),
        (TYPE_DAILY_ALERT, _('Daily reports by email')),
    )

    user = models.ForeignKey(AUTH_USER_MODEL, on_delete=models.CASCADE)
    topic = models.ForeignKey(get_model_string('Topic'), on_delete=models.CASCADE)
    type = models.PositiveSmallIntegerField(default=TYPE_NO_ALERT, db_index=True, choices=TYPE_CHOICES)
    created = models.DateTimeField(_('Created'), null=True, auto_now_add=True)
    updated = models.DateTimeField(_('Updated'), null=True, blank=True)
    sent = models.BooleanField(default=False)

    objects = SubscriptionManager()

    class Meta(object):
        ordering = ['-created']
        verbose_name = _('Subscription')
        verbose_name_plural = _('Subscriptions')
        app_label = 'pybb'
        abstract = True


class BaseTopic(ParentForumBase):
    MODERATION_IS_CLEAN = 0
    MODERATION_IS_IN_MODERATION = 1
    MODERATION_HAS_POSTS_IN_MODERATION = 2
    MODERATION_CHOICES = (
        (MODERATION_IS_CLEAN, _('Topic is clean')),
        (MODERATION_IS_IN_MODERATION, _('Topic is in moderation')),
        (MODERATION_HAS_POSTS_IN_MODERATION, _('Topic has posts in moderation')),
    )

    forum = models.ForeignKey(get_model_string('Forum'), related_name='topics', verbose_name=_('Forum'), on_delete=models.PROTECT)
    name = models.CharField(_('Subject'), max_length=255)
    slug = AutoSlugField(populate_from='name', max_length=255)

    cover = ImageField(_('Cover'), blank=True, null=True,
                       upload_to=defaults.PYBB_COVER_UPLOAD_TO)

    created = models.DateTimeField(_('Created'), null=True)
    updated = models.DateTimeField(_('Updated'), null=True)
    user = models.ForeignKey(AUTH_USER_MODEL, verbose_name=_('User'), on_delete=models.SET(AnonymousUser))
    views = models.IntegerField(_('Views count'), blank=True, default=0, db_index=True)
    sticky = models.BooleanField(_('Sticky'), blank=True, default=False, db_index=True)
    closed = models.BooleanField(_('Closed'), blank=True, default=False, db_index=True)
    redirect = models.BooleanField(_('Redirect'), blank=True, default=False, db_index=True)
    deleted = models.BooleanField(_('Deleted'), default=False)
    subscribers = models.ManyToManyField(AUTH_USER_MODEL,
                                         related_name='subscriptions',
                                         verbose_name=_('Subscribers'),
                                         blank=True,
                                         through=get_model_string('Subscription'))
    post_count = models.IntegerField(_('Post count'), blank=True, null=False, default=0, db_index=True)
    member_count = models.IntegerField(_('Member count'), blank=True, null=False, default=0, db_index=True)
    readed_by = models.ManyToManyField(AUTH_USER_MODEL, through=get_model_string('TopicReadTracker'), related_name='readed_topics')
    on_moderation = models.IntegerField(_('On moderation'), default=MODERATION_IS_CLEAN, db_index=True)
    first_post = models.ForeignKey(get_model_string('Post'),
                                   blank=True,
                                   null=True,
                                   related_name='first_posts',
                                   on_delete=models.SET_NULL)

    last_post = models.ForeignKey(get_model_string('Post'),
                                  blank=True,
                                  null=True,
                                  related_name='last_topics',
                                  on_delete=models.SET_NULL)

    poll = models.ForeignKey(get_model_string('Poll'),
                             blank=True,
                             null=True,
                             on_delete=models.SET_NULL,
                             related_name='topics')

    objects = TopicManager()

    class Meta(object):
        ordering = ['-created']
        verbose_name = _('Topic')
        verbose_name_plural = _('Topics')
        app_label = 'pybb'
        abstract = True

    def get_last_page(self):
        return int(math.ceil(self.post_count / float(defaults.PYBB_TOPIC_PAGE_SIZE)))

    def __str__(self):
        return self.name

    def absorb(self, topic, redirection_type=None, expired=None):
        from pybb.models import TopicRedirection

        if not redirection_type:
            redirection_type = TopicRedirection.TYPE_PERMANENT_REDIRECT

        topic.posts.all().update(topic=self)
        topic.subscription_set.all().update(topic=self)

        if topic.poll:
            poll = topic.poll

            topic.poll = None

            self.poll = poll

            update_fields(self, fields=('poll', ))

        topic.topicreadtracker_set.all().update(topic=self)

        topic.redirect = True

        update_fields(topic, fields=('redirect', ))

        TopicRedirection.objects.create(from_topic=topic,
                                        to_topic=self,
                                        type=redirection_type,
                                        expired=expired)

        topic.update_counters()
        self.update_counters()

    @property
    def head(self):
        return self.get_first_post()

    def get_first_post(self, force_refresh=False, select_related=None, prefetch_related=None):
        """
        Get first post and cache it for request
        """
        if self.first_post_id and not (force_refresh or select_related or prefetch_related):
            self._head = self.first_post

        if not getattr(self, '_head', None) or force_refresh:
            qs = self.posts.order_by('created')[:1]

            if select_related:
                qs = qs.select_related(*select_related)
            if prefetch_related:
                qs = qs.prefetch_related(*prefetch_related)
            try:
                self._head = qs[0]
            except IndexError:
                return None
            else:
                self.first_post = self._head

        self._head.topic = self

        return self._head

    def get_last_post(self, select_related=None, prefetch_related=None):
        last_post = (self.posts
                     .visible(join=False)
                     .annotate(last_saved=Greatest('created', 'updated'))
                     .order_by(F('last_saved').desc(nulls_last=True)))

        if select_related:
            last_post = last_post.select_related(*select_related)

        if prefetch_related:
            last_post = last_post.prefetch_related(*prefetch_related)

        try:
            last_post = last_post[0]
        except IndexError:
            return None
        else:
            last_post.topic = self
            self.last_post = last_post
            return last_post

    def mark_as_read(self, user):
        from pybb.models import TopicReadTracker, ForumReadTracker, Subscription, Topic

        Subscription.objects.filter(topic=self, user=user).update(sent=False)

        try:
            forum_mark = ForumReadTracker.objects.get(forum=self.forum, user=user)
        except ObjectDoesNotExist:
            forum_mark = None

        if self.updated and ((forum_mark is None) or (forum_mark.time_stamp < self.updated)):
            # Mark topic as read
            count = TopicReadTracker.objects.filter(topic=self, user=user).update(time_stamp=tznow())

            if not count:
                TopicReadTracker.objects.create(topic=self, user=user)

            # Check, if there are any unread topics in forum
            read = Topic.objects.filter(
                forum=self.forum, topicreadtracker__user=user, topicreadtracker__time_stamp__gt=F('updated'))

            unread = Topic.objects.filter(forum=self.forum).exclude(id__in=read)
            if forum_mark:
                unread = unread.filter(updated__gt=forum_mark.time_stamp)

            if not unread.exists():
                # Clear all topic marks for this forum, mark forum as read
                TopicReadTracker.objects.filter(
                    user=user,
                    topic__forum=self.forum
                ).delete()

                if not forum_mark:
                    ForumReadTracker.objects.create(forum=self.forum, user=user)

                else:
                    forum_mark.time_stamp = tznow()
                    update_fields(forum_mark, fields=('time_stamp', ))

    def mark_as_deleted(self, commit=True, update=True):
        self.deleted = True

        self.posts.visible(join=False).update(deleted=True)

        if commit:
            update_fields(self, fields=('deleted', ))

        if update:
            self.forum.update_counters(commit=commit)

    def mark_as_undeleted(self, commit=True, update=True):
        self.deleted = False

        post_ids = (PostDeletion.objects
                    .filter(post__topic=self)
                    .exclude(post_id=self.first_post_id)
                    .values_list('post', flat=True))

        self.posts.exclude(pk__in=post_ids).update(deleted=False)

        if commit:
            update_fields(self, fields=('deleted', ))

        if update:
            self.forum.update_counters(commit=commit)

    def get_absolute_url(self, page=None):
        kwargs = {
            'pk': self.id,
            'slug': self.slug,
            'forum_slug': self.forum.slug
        }

        if page and page != 1:
            kwargs['page'] = page

        return reverse('pybb:topic_detail', kwargs=kwargs)

    def save(self, *args, **kwargs):
        if self.id is None:
            self.created = tznow()

        super(BaseTopic, self).save(*args, **kwargs)

    def update_counters(self, commit=True, update_forum=True):
        self.post_count = self.posts.visible(join=False).count()

        active_members = self.posts.visible(join=False).values('user_id').order_by()
        active_members.query.group_by = ('user_id', )
        self.member_count = len(active_members)

        last_post = self.get_last_post()

        if last_post:
            self.updated = last_post.updated or last_post.created
            self.last_post = last_post

        first_post = self.get_first_post(force_refresh=True)

        if first_post:
            self.first_post = first_post

        if self.posts.filter(on_moderation=True, deleted=False).count():
            if not self.post_count or first_post.on_moderation is True:
                self.on_moderation = self.MODERATION_IS_IN_MODERATION
            else:
                self.on_moderation = self.MODERATION_HAS_POSTS_IN_MODERATION
        else:
            self.on_moderation = self.MODERATION_IS_CLEAN

        if commit:
            self.save(update_fields=['poll_id', 'post_count', 'member_count', 'updated', 'last_post', 'first_post', 'on_moderation'])

        if update_forum:
            self.forum.update_counters(commit=commit)

    @property
    def poll_votes(self):
        return self.poll.poll_votes

    def get_poll_answers(self):
        if self.poll:
            self.poll.topic = self

            poll_answers = self.poll.answers.all().prefetch_related('users')

            for poll_answer in poll_answers:
                poll_answer.poll = self.poll

            return poll_answers

        return None

    def is_moderated_by(self, user, permission=None):
        return self.forum.is_moderated_by(user, permission=permission)

    def is_accessible_by(self, user):
        if ((self.on_moderation == self.MODERATION_IS_IN_MODERATION or self.deleted) and
            not self.is_moderated_by(user) and
                (not user.is_authenticated or
                 (user.is_authenticated and
                  not user.pk == self.user_id))):
            return False

        return self.forum.is_accessible_by(user)

    def is_subscribed_by(self, user):
        return (user.is_authenticated and
                user.pk in self.subscribers.values_list('id', flat=True))

    def is_hidden(self):
        return self.forum.is_hidden()

    def sync_cover(self, commit=True, force=False):
        if not self.first_post_id:
            return False

        if force is False and self.cover:
            return False

        for url in self.first_post.images:
            response = requests.get(url)

            if response.status_code == 200:
                file_ext = urlparse(url).path.split('.')[-1]

                self.cover.save("%s.%s" % (self.id, file_ext),
                                ContentFile(response.content), save=True)

                if commit:
                    update_fields(self, fields=('cover', ))

                return True

        return False


class RenderableItem(ModelBase):
    """
    Base class for models that has markup, body, body_text and body_html fields.
    """

    class Meta(object):
        abstract = True

    body = models.TextField(_('Message'), null=True)
    body_html = models.TextField(_('HTML version'), null=True)
    body_text = models.TextField(_('Text version'), null=True)

    def render(self, commit=False):
        self.body_html = markup(self.body, obj=self)

        # Remove tags which was generated with the markup processor
        text = strip_tags(self.body_html)
        # Unescape entities which was generated with the markup processor
        self.body_text = unescape(text)

        if commit:
            update_fields(self, fields=('body_html', 'body_text', ))

    def get_body_html(self, asynchronous=True, force=False):
        if self.body_html is not None and not force:
            return self.body_html

        if asynchronous:
            generate_markup.delay(self.pk)

            return None

        self.render()

        return self.body_html

    def get_body_text(self, asynchronous=True, force=False):
        if self.body_text is not None and not force:
            return self.body_text

        if asynchronous:
            generate_markup.delay(self.pk)

            return None

        self.render()

        return self.body_text


class PostQuerySetMixin(object):
    def filter_by_user(self, topic, user):
        if not topic.is_moderated_by(user):
            if user.is_authenticated:
                return (self.filter(Q(user=user) | Q(on_moderation=False))
                        .exclude(deleted=True))

            return self.filter(on_moderation=False).exclude(deleted=True)

        return self

    def visible(self, join=True):
        filters = {
            'on_moderation': False,
            'deleted': False
        }

        if join:
            filters = dict(filters, **{
                'topic__deleted': False,
                'topic__redirect': False
            })

        return self.filter(**filters)


class PostQuerySet(PostQuerySetMixin, QuerySetBase):
    pass


@queryset
class PostManager(ManagerBase):
    def get_queryset(self):
        return PostQuerySet(self.model)

    def filter_by_user(self, topic, user):
        return self.get_queryset().filter_by_user(topic, user)

    def visible(self, join=True):
        return self.get_queryset().visible(join)

    def contribute_to_class(self, cls, name):
        signals.post_save.connect(self.post_save, sender=cls)
        return super(PostManager, self).contribute_to_class(cls, name)

    def post_save(self, instance, **kwargs):
        if defaults.PYBB_NOTIFICATION_ENABLE and kwargs.get('created', False):
            notify_topic_subscribers(instance)

        if defaults.PYBB_ATTACHMENT_ENABLE:
            from pybb.models import Attachment
            Attachment.objects.filter(post_hash=instance.hash,
                                      post__isnull=True).update(post=instance)


class PostDeletion(ModelBase):
    user = models.ForeignKey(AUTH_USER_MODEL, related_name='posts_deletion', on_delete=models.SET(AnonymousUser))
    post = models.OneToOneField(get_model_string('Post'), related_name='deletion', on_delete=models.CASCADE)
    created = models.DateTimeField(_('Created'), auto_now_add=True)

    class Meta(object):
        ordering = ['-created']
        app_label = 'pybb'


class BasePost(RenderableItem):
    topic = models.ForeignKey(get_model_string('Topic'),
                              related_name='posts',
                              verbose_name=_('Topic'),
                              on_delete=models.CASCADE)
    user = models.ForeignKey(AUTH_USER_MODEL, related_name='posts', verbose_name=_('User'), on_delete=models.SET(AnonymousUser))
    created = models.DateTimeField(_('Created'), blank=True)
    updated = models.DateTimeField(_('Updated'), blank=True, null=True)
    user_ip = models.GenericIPAddressField(_('User IP'),
                                           blank=True,
                                           default='0.0.0.0',
                                           null=True)
    on_moderation = models.BooleanField(_('On moderation'), default=False, db_index=True)

    deleted = models.BooleanField(_('Deleted'), default=False, db_index=True)

    hash = models.CharField(max_length=150, null=True, blank=True, db_index=True)

    objects = PostManager()

    class Meta(object):
        ordering = ['-created']
        verbose_name = _('Post')
        verbose_name_plural = _('Posts')
        app_label = 'pybb'
        abstract = True

    def summary(self):
        if self.body:
            LIMIT = 50
            tail = len(self.body) > LIMIT and '...' or ''
            return self.body[:LIMIT] + tail

        return ''

    __str__ = summary

    def get_attachments(self):
        if hasattr(self, '_attachments'):
            return self._attachments

        return self.attachments.all()

    def is_accessible_by(self, user):
        if (defaults.PYBB_PREMODERATION and self.on_moderation and
            (not self.topic.is_moderated_by(user)) and
                (not user.is_authenticated or not self.user_id == user.pk)):
            return False

        return self.topic.is_accessible_by(user)

    def is_editable_by(self, user, permission=None):
        if self.is_posted_by(user):
            return True

        if user.is_superuser or user.is_staff:
            return True

        if self.topic.is_moderated_by(user, permission=permission):
            return True

        return False

    def get_hash(self):
        if self.pk and self.hash:
            return self.hash

        return uuid.uuid4()

    def is_posted_by(self, user):
        return self.user_id == user.pk

    def save(self, *args, **kwargs):
        new = self.pk is None

        created_at = tznow()

        if self.created is None:
            self.created = created_at

        if (new or 'body' in self._initial_attr and
                self.body != self._initial_attr['body']):
            self.render()

        super(BasePost, self).save(*args, **kwargs)

        if new:
            self.topic.updated = created_at
            self.topic.forum.updated = created_at

        self.topic.update_counters()

        if new and self.pk == self.topic.first_post_id:
            sync_cover.delay(self.topic_id)

    def get_absolute_url(self):
        return self.get_anchor_url()

    def get_page_index(self, user=None):
        if not user:
            user = AnonymousUser()

        count = self.topic.posts.filter_by_user(self.topic, user).filter(created__lt=self.created).count() + 1

        page = math.ceil(count / float(defaults.PYBB_TOPIC_PAGE_SIZE))

        return int(page)

    def get_anchor_url(self, user=None, params=None):
        return '%s%s#post%d' % (
            self.topic.get_absolute_url(int(self.get_page_index(user))),
            '?%s' % urlencode(params) if params else '',
            self.id
        )

    def mark_as_deleted(self, commit=True, user=None):
        self_id = self.id

        self.deleted = True

        if user:
            PostDeletion.objects.create(post=self, user=user)

        try:
            head_post_id = self.topic.posts.visible(join=False).order_by('created')[0].id
        except IndexError:
            pass
        else:
            if self_id == head_post_id:
                self.topic.mark_as_deleted()

        if commit:
            update_fields(self, fields=('deleted', ))

    def mark_as_undeleted(self, commit=True):
        self_id = self.id

        self.deleted = False

        PostDeletion.objects.filter(post=self).delete()

        if commit:
            update_fields(self, fields=('deleted', ))

        try:
            head_post_id = self.topic.posts.visible(join=False).order_by('created')[0].id
        except IndexError:
            pass
        else:
            if self_id == head_post_id:
                self.topic.mark_as_undeleted()

    @cached_property
    def parents(self):
        """
        Used in templates for breadcrumb building
        """
        return self.topic.parents + [self.topic, ]

    def is_updatable(self):
        delta = (tznow() - self.created).seconds

        return delta > defaults.PYBB_UPDATE_MENTION_POST_DELTA

    @classmethod
    def watch_topic(cls, old_attr={}, new_attr={},
                    instance=None, sender=None, **kw):

        from pybb.models import Topic

        if (old_attr.get('topic_id', None) and
                old_attr.get('topic_id') != new_attr.get('topic_id')):
            instance.save(_signal=False)

            try:
                Topic.objects.get(pk=old_attr.get('topic_id')).update_counters()
            except ObjectDoesNotExist:
                pass

    @property
    def images(self):
        if self.body_html:
            soup = BeautifulSoup(self.body_html, 'lxml')

            for img in soup.findAll('img'):
                if img.get('src'):
                    yield img['src']


class BaseAttachment(ModelBase):
    TYPE_IMAGE = 1
    TYPE_APPLICATION = 2
    TYPE_ARCHIVE = 3
    TYPE_TEXT = 4
    TYPE_BINARY = 5
    TYPE_CHOICES = (
        (TYPE_IMAGE, _('image')),
        (TYPE_APPLICATION, _('application')),
        (TYPE_ARCHIVE, _('archive')),
        (TYPE_TEXT, _('text')),
        (TYPE_BINARY, _('binary')),
    )

    TYPE_EXTENSIONS = {
        TYPE_IMAGE: ('gif', 'jpeg', 'jpg', 'png', ),
        TYPE_APPLICATION: ('doc', 'docx', 'pdf', 'psd', ),
        TYPE_ARCHIVE: ('zip', 'rar', ),
        TYPE_TEXT: ('txt', 'rtf', ),
        TYPE_BINARY: ('bmp', )
    }

    MIMETYPES = (
        'application/pdf',
        'image/jpeg',
        'image/jpg',
        'image/png',
        'application/zip',
        'application/gzip',
        'image/gif',
        'image/pjpeg',
        'text/plain',
    )

    post = models.ForeignKey(get_model_string('Post'),
                             verbose_name=_('Post'),
                             related_name='attachments',
                             null=True, blank=True, on_delete=models.CASCADE)
    size = models.IntegerField(_('Size'))
    file = ContentTypeRestrictedFileField(_('File'),
                                          upload_to=defaults.PYBB_ATTACHMENT_UPLOAD_TO,
                                          content_types=MIMETYPES,
                                          max_upload_size=5242880,
                                          null=True, blank=True)
    filename = models.CharField(max_length=100)
    post_hash = models.CharField(max_length=150, null=True, blank=True)
    extension = models.CharField(max_length=20)
    mimetype = models.CharField(max_length=255, db_index=True, null=True, blank=True)
    type = models.PositiveSmallIntegerField(db_index=True, choices=TYPE_CHOICES)
    visible = models.BooleanField(default=True)
    counter = models.PositiveIntegerField(default=0)
    created = models.DateTimeField(_('Created'), auto_now_add=True)
    user = models.ForeignKey(AUTH_USER_MODEL, on_delete=models.SET(AnonymousUser))

    class Meta(object):
        verbose_name = _('Attachment')
        verbose_name_plural = _('Attachments')
        app_label = 'pybb'
        ordering = ['-created', ]
        abstract = True

    def save(self, *args, **kwargs):
        self.size = self.file.size
        self.filename = self.file.name
        filename, self.extension = os.path.splitext(self.file.path)

        self.extension = self.extension[1:]

        self.type = self.get_type_by_extension(self.extension.lower())

        mg = magic.Magic(mime=True)
        self.mimetype = mg.from_buffer(self.file.read(1024))

        super(BaseAttachment, self).save(*args, **kwargs)

    def get_type_by_extension(self, ext):
        results = list(filter(lambda ind: ext in ind[1], self.TYPE_EXTENSIONS.items()))

        if len(results):
            return results[0][0]

        return None

    def size_display(self):
        size = self.size
        if size < 1024:
            return '%db' % size
        elif size < 1024 * 1024:
            return '%dKb' % int(size / 1024)

        return '%.2fMb' % (size / float(1024 * 1024))

    def is_type_image(self):
        return self.type == self.TYPE_IMAGE

    def is_type_application(self):
        return self.type == self.TYPE_APPLICATION

    def is_type_text(self):
        return self.type == self.TYPE_TEXT

    def is_type_archive(self):
        return self.type == self.TYPE_ARCHIVE


class BaseTopicReadTracker(ModelBase):
    """
    Save per user topic read tracking
    """
    class Meta(object):
        verbose_name = _('Topic read tracker')
        verbose_name_plural = _('Topic read trackers')
        app_label = 'pybb'
        abstract = True

    user = models.ForeignKey(AUTH_USER_MODEL, blank=False, null=False, on_delete=models.CASCADE)
    topic = models.ForeignKey(get_model_string('Topic'),
                              blank=True,
                              null=True,
                              on_delete=models.CASCADE)
    time_stamp = models.DateTimeField(auto_now=True)


class ForumReadTrackerManager(ManagerBase):
    def mark_as_read(self, user, forums):
        forum_ids = [tracker[0]
                     for tracker in self.filter(user=user).values_list('forum_id')]

        trackers = []
        updated_ids = []

        for forum in forums:
            if forum.pk not in forum_ids:
                trackers.append(self.model(forum=forum, user=user))
            else:
                updated_ids.append(forum.pk)

        if len(trackers):
            trackers = self.bulk_create(trackers)

        if len(updated_ids):
            self.filter(forum__in=updated_ids, user=user).update(time_stamp=tznow())

        return trackers


class BaseForumReadTracker(ModelBase):
    """
    Save per user forum read tracking
    """
    class Meta(object):
        verbose_name = _('Forum read tracker')
        verbose_name_plural = _('Forum read trackers')
        app_label = 'pybb'
        abstract = True

    user = models.ForeignKey(AUTH_USER_MODEL, blank=False, null=False, on_delete=models.CASCADE)
    forum = models.ForeignKey(get_model_string('Forum'), blank=True, null=True, on_delete=models.CASCADE)
    time_stamp = models.DateTimeField(auto_now=True)

    objects = ForumReadTrackerManager()


class BasePoll(ModelBase):
    TYPE_NONE = 0
    TYPE_SINGLE = 1
    TYPE_MULTIPLE = 2

    TYPE_CHOICES = (
        (TYPE_NONE, _('None')),
        (TYPE_SINGLE, _('Single answer')),
        (TYPE_MULTIPLE, _('Multiple answers')),
    )

    type = models.PositiveSmallIntegerField(_('Poll type'),
                                            choices=TYPE_CHOICES,
                                            default=TYPE_NONE,
                                            db_index=True)
    question = models.TextField(_('Poll question'), blank=True, null=True)

    created = models.DateTimeField(_('Created'), auto_now_add=True)
    updated = models.DateTimeField(_('Updated'), null=True)
    active = models.BooleanField(db_index=True, default=True)
    public = models.BooleanField(db_index=True, default=True)
    timeout = models.PositiveIntegerField(null=True, blank=True)

    class Meta(object):
        verbose_name = _('Poll')
        verbose_name_plural = _('Polls')
        app_label = 'pybb'
        abstract = True
        ordering = ['-updated', '-created']

    @cached_property
    def poll_votes(self):
        result = self.answers.aggregate(total=models.Sum('user_count'))

        return result['total'] or 0

    def mark_updated(self):
        now = tznow()

        self.updated = now
        update_fields(self, fields=('updated', ))

        for topic in self.topics.all():
            topic.updated = now
            update_fields(topic, fields=('updated', ))


class BasePollAnswer(ModelBase):
    poll = models.ForeignKey(get_model_string('Poll'),
                             related_name='answers',
                             verbose_name=_('Poll'),
                             on_delete=models.CASCADE)
    text = models.CharField(max_length=255,
                            verbose_name=_('Text'))

    user_count = models.PositiveIntegerField(default=0, db_index=True)

    class Meta:
        verbose_name = _('Poll answer')
        verbose_name_plural = _('Polls answers')
        app_label = 'pybb'
        abstract = True

    def __str__(self):
        return self.text

    def votes(self):
        return self.users.count()

    def votes_percent(self):
        poll_votes = self.poll.poll_votes

        if poll_votes > 0:
            return int(1.0 * self.votes() / poll_votes * 100)

        return 0

    def compute(self, commit=True):
        self.user_count = self.votes()

        if commit:
            update_fields(self, fields=('user_count', ))


class PollAnswerUserManager(ManagerBase):
    def contribute_to_class(self, cls, name):
        signals.post_save.connect(self.post_save, sender=cls)
        signals.post_delete.connect(self.post_delete, sender=cls)
        return super(PollAnswerUserManager, self).contribute_to_class(cls, name)

    def post_delete(self, instance, **kwargs):
        try:
            instance.poll_answer.compute()
        except ObjectDoesNotExist:
            pass

    def post_save(self, instance, **kwargs):
        instance.poll_answer.compute()


class BasePollAnswerUser(ModelBase):
    poll_answer = models.ForeignKey(get_model_string('PollAnswer'),
                                    related_name='users',
                                    verbose_name=_('Poll answer'),
                                    on_delete=models.CASCADE)
    user = models.ForeignKey(AUTH_USER_MODEL,
                             related_name='poll_answers',
                             verbose_name=_('User'),
                             on_delete=models.SET(AnonymousUser))
    created = models.DateTimeField(auto_now_add=True)

    objects = PollAnswerUserManager()

    class Meta:
        verbose_name = _('Poll answer user')
        verbose_name_plural = _('Polls answers users')
        app_label = 'pybb'
        abstract = True
        ordering = ['-created']

    def __str__(self):
        return '%s - %s' % (self.poll_answer.topic, self.user)

    def save(self, *args, **kwargs):
        super(BasePollAnswerUser, self).save(*args, **kwargs)

        self.poll_answer.poll.mark_updated()


class LogModerationManager(ManagerBase):
    def log(self, user, obj, action_flag, change_message='', level=None,
            target=None, user_ip=None, commit=True):

        e = self.model()
        e.user = user
        e.content_object = obj
        e.action_flag = action_flag
        e.change_message = change_message
        e.user_ip = user_ip
        e.target = target

        if level:
            e.level = level

        if commit:
            e.save()


class BaseLogModeration(ModelBase):
    ACTION_FLAG_ADDITION = 1
    ACTION_FLAG_CHANGE = 2
    ACTION_FLAG_DELETION = 3
    ACTION_FLAG_BAN = 4
    ACTION_FLAG_UNBAN = 5
    ACTION_FLAG_CHOICES = (
        (ACTION_FLAG_ADDITION, _('addition')),
        (ACTION_FLAG_DELETION, _('deletion')),
        (ACTION_FLAG_CHANGE, _('change')),
        (ACTION_FLAG_BAN, _('ban')),
        (ACTION_FLAG_UNBAN, _('unban')),
    )

    LEVEL_LOW = 1
    LEVEL_MEDIUM = 2
    LEVEL_HIGH = 3
    LEVEL_CHOICES = (
        (LEVEL_LOW, _('low')),
        (LEVEL_MEDIUM, _('medium')),
        (LEVEL_HIGH, _('high')),
    )

    action_time = models.DateTimeField(_('action time'), auto_now=True)
    user = models.ForeignKey(AUTH_USER_MODEL, related_name='logs', on_delete=models.SET(AnonymousUser))
    target = models.ForeignKey(AUTH_USER_MODEL, blank=True, null=True, related_name='target_logs', on_delete=models.SET_NULL)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField(_('object id'), db_index=True)
    content_object = GenericForeignKey(ct_field='content_type', fk_field='object_id')
    action_flag = models.PositiveSmallIntegerField(_('action flag'),
                                                   choices=ACTION_FLAG_CHOICES,
                                                   db_index=True)
    change_message = models.TextField(_('change message'), blank=True)
    level = models.PositiveSmallIntegerField(_('level'),
                                             choices=LEVEL_CHOICES,
                                             default=LEVEL_LOW,
                                             db_index=True)
    user_ip = models.GenericIPAddressField(_('User IP'),
                                           blank=True,
                                           default='0.0.0.0',
                                           null=True)

    objects = LogModerationManager()

    class Meta:
        app_label = 'pybb'
        verbose_name = _('Log moderation')
        verbose_name_plural = _('Logs moderation')
        ordering = ['-action_time']
        abstract = True

    def is_action_flag_addition(self):
        return self.action_flag == self.ACTION_FLAG_ADDITION

    def is_action_flag_change(self):
        return self.action_flag == self.ACTION_FLAG_CHANGE

    def is_action_flag_deletion(self):
        return self.action_flag == self.ACTION_FLAG_DELETION

    def is_action_flag_ban(self):
        return self.action_flag == self.ACTION_FLAG_BAN

    def is_action_flag_unban(self):
        return self.action_flag == self.ACTION_FLAG_UNBAN

    def is_level_low(self):
        return self.level == self.LEVEL_LOW

    def is_level_medium(self):
        return self.level == self.LEVEL_MEDIUM

    def is_level_high(self):
        return self.level == self.LEVEL_HIGH

    def __str__(self):
        if self.is_action_flag_addition():
            if self.target:
                return _('Added %(target)s to "%(object)s."') % {
                    'object': self.content_object,
                    'target': self.target
                }

            return _('Added "%(object)s".') % {
                'object': self.content_object
            }

        if self.is_action_flag_change():
            if self.target:
                return _('Changed %(target)s to "%(object)s."') % {
                    'object': self.content_object,
                    'target': self.target
                }
            return _('Changed "%(object)s" - %(changes)s') % {
                'object': self.content_object,
                'changes': self.change_message
            }

        if self.is_action_flag_deletion():
            if self.target:
                return _('Deleted %(target)s from "%(object)s."') % {
                    'object': self.content_object,
                    'target': self.target
                }

            return _('Deleted "%(object)s."') % {
                'object': self.content_object
            }

        return _('LogModeration object')

    def __repr__(self):
        return smart_text(self.action_time)


def get_user_timezone(user):
    return defaults.PYBB_DEFAULT_TIME_ZONE
