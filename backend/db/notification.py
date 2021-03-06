# Copyright 2012 Viewfinder Inc. All Rights Reserved.

"""Viewfinder notification.

  Notifications are the mechanism by which the client is notified of incremental modifications
  to server assets. Notifications enable the client to incrementally keep its state in sync
  with the server. Furthermore, pushed alerts notify end users that their client does not
  have the latest data.

  *Every* change that is visible to a particular user results in the creation of a notification.
  Certain kinds of operations, such as share, also result in the push of an alert to all the
  user's devices. Operations which modify viewpoint assets in ways that are visible to other
  followers also result in the creation of an activity.

  Although at first glance notifications and activities are similar, they are different in
  important ways:

    1. Notifications are created *per-user*, and apply to any change that may have occurred in
       assets viewable by that user. In contrast, activities are associated with viewpoints,
       not users, and are only created for changes that are visible to all followers. As an
       example, a user might override the title of a viewpoint. A notification is created, but
       an activity is not, since that change is user-specific and not visible to other followers.

    2. Notifications contain coarse-granularity invalidation lists, which instruct the client as
       to which assets need to be re-queried. In contrast, activities contain the exhaustive list
       of operation-specific asset identifiers which were changed. As an example, a notification
       generated by a share might contain just a single episode invalidation, whereas the
       corresponding activity would contain the identifier of that episode *plus* the identifiers
       of all photos shared as part of that episode.

    3. The notification table will be truncated periodically, whereas the activity table lives
       indefinitely. The notification table exists in order to allow the client to incrementally
       update its cache of server state. The activity table exists in order to keep a record of
       structural changes to the viewpoint.

  Notification: Notify client of changes to asset tree.
"""

__authors__ = ['spencer@emailscrubbed.com (Spencer Kimball)',
               'andy@emailscrubbed.com (Andy Kimball)']

import json
import logging

from tornado import gen
from viewfinder.backend.base import util
from viewfinder.backend.db import vf_schema
from viewfinder.backend.db.base import DBObject
from viewfinder.backend.db.range_base import DBRangeObject

@DBObject.map_table_attributes
class Notification(DBRangeObject):
  """Viewfinder notification data object."""
  __slots__ = []

  _table = DBObject._schema.GetTable(vf_schema.NOTIFICATION)

  def __init__(self, user_id=None, notification_id=None):
    super(Notification, self).__init__()
    self.user_id = user_id
    self.notification_id = notification_id

  def GetInvalidate(self):
    """Parses and returns the JSON invalidate attribute as a python dict."""
    return json.loads(self.invalidate) if self.invalidate is not None else None

  def SetInvalidate(self, invalidate_dict):
    """Sets invalidation python dict as JSON invalidate attribute."""
    self.invalidate = json.dumps(invalidate_dict)

  @classmethod
  @gen.coroutine
  def TryClearBadge(cls, client, user_id, device_id, notification_id):
    """Tries to create a "clear_badges" notification with the given id. Returns False if another
    notification with this id has already been created, else returns True.
    """
    notification = Notification(user_id, notification_id)
    notification.name = 'clear_badges'
    notification.timestamp = util.GetCurrentTimestamp()
    notification.sender_id = user_id
    notification.sender_device_id = device_id
    notification.badge = 0

    # If _TryUpdate returns false, then new notifications showed up while the query was running, and so
    # retry creation of the notification. 
    success = yield notification._TryUpdate(client)
    raise gen.Return(success)

  @classmethod
  @gen.coroutine
  def QueryLast(cls, client, user_id, consistent_read=False):
    """Returns the notification with the highest notification_id, or None if the notification
    table is empty.
    """
    notification_list = yield gen.Task(Notification.RangeQuery,
                                       client,
                                       user_id,
                                       range_desc=None,
                                       limit=1,
                                       col_names=None,
                                       scan_forward=False,
                                       consistent_read=consistent_read)

    raise gen.Return(notification_list[0] if len(notification_list) > 0 else None)

  @classmethod
  @gen.coroutine
  def CreateForUser(cls, client, operation, user_id, name, invalidate=None,
                    activity_id=None, viewpoint_id=None, seq_num_pair=None,
                    inc_badge=False, consistent_read=False):
    """Creates a notification database record for the specified user, based upon the
    notification record that was last created and the current operation. If "inc_badge" is
    true, then increment the user's pending notification badge count. Returns the newly
    created notification.
    """
    while True:
      last_notification = yield Notification.QueryLast(client, user_id, consistent_read=consistent_read)

      if last_notification is None:
        notification_id = 1
        badge = 0
      else:
        notification_id = last_notification.notification_id + 1
        badge = last_notification.badge

      notification = Notification(user_id, notification_id)
      notification.name = name
      if invalidate is not None:
        notification.SetInvalidate(invalidate)
      notification.activity_id = activity_id
      notification.viewpoint_id = viewpoint_id

      # Store update_seq and/or viewed_seq on notification if they were specified.
      if seq_num_pair is not None:
        update_seq, viewed_seq = seq_num_pair
        notification.update_seq = update_seq

        # viewed_seq applies only to the user that submitted the operation.
        if viewed_seq is not None and operation.user_id == user_id:
          notification.viewed_seq = viewed_seq

      # Increment badge if requested to do so.
      if inc_badge:
        badge += 1
      notification.badge = badge
      notification.timestamp = operation.timestamp
      notification.sender_id = operation.user_id
      notification.sender_device_id = operation.device_id
      notification.op_id = operation.operation_id

      success = yield notification._TryUpdate(client)

      # If creation of the notification succeeded, then query is complete. Otherwise, retry from
      # start since another notification allocated the same id.
      if success:
        raise gen.Return(notification)

      # If update failed, may have been because we couldn't read the "real" last notification.
      consistent_read = True

  @gen.coroutine
  def _TryUpdate(self, client):
    """Creates a new notification database record using the next available notification_id.
    Avoids race conditions by using the "expected" argument to Update in order to ensure that
    a unique notification_id is used. If another notification allocates a particular
    notification_id first, this method will return False. The caller can then retry with a new
    notification_id.
    """
    try:
      yield gen.Task(self.Update, client, expected={'notification_id': False})
    except Exception as e:
      # Notification creation failed, so return False so caller can retry.
      logging.info('notification id %d is already in use: %s' % (self.notification_id, e))
      raise gen.Return(False)

    raise gen.Return(True)
