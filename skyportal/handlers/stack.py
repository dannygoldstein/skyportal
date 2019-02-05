from baselayer.app.handlers.base import BaseHandler
from baselayer.app.access import auth_or_token
from skyportal.models import ForcedPhotometry
from .. import stack

import tornado.web


# TODO this should distinguish between "no data to plot" and "plot failed"
class StackHandler(BaseHandler):
    @auth_or_token
    def get(self, source_id, stack_binsize=None):

        if stack_binsize is not None and stack_binsize < 1:
            self.error(f'Invalid binsize "{stack_binsize}" days. Can either be null or at least 1.')

        info = ForcedPhotometry.get_if_owned_by(source_id, self.current_user)

        if stack_binsize is None:
            self.success(info)

        else:
            try:
                stacked = stack(info, binsize=stack_binsize)
            except Exception as e:
                self.error(e.msg)
            else:
                self.success(stacked)
