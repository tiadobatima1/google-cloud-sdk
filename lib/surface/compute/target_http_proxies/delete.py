# -*- coding: utf-8 -*- #
# Copyright 2014 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Command for deleting target HTTP proxies."""

from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

from googlecloudsdk.api_lib.compute import base_classes
from googlecloudsdk.api_lib.compute import utils
from googlecloudsdk.calliope import base
from googlecloudsdk.command_lib.compute import flags as compute_flags
from googlecloudsdk.command_lib.compute.target_http_proxies import flags
from googlecloudsdk.command_lib.compute.target_http_proxies import target_http_proxies_utils


@base.ReleaseTracks(base.ReleaseTrack.GA, base.ReleaseTrack.BETA)
class Delete(base.DeleteCommand):
  """Delete target HTTP proxies.

  *{command}* deletes one or more target HTTP proxies.
  """

  TARGET_HTTP_PROXY_ARG = None

  @staticmethod
  def Args(parser):
    Delete.TARGET_HTTP_PROXY_ARG = flags.TargetHttpProxyArgument(plural=True)
    Delete.TARGET_HTTP_PROXY_ARG.AddArgument(parser, operation_type='delete')
    parser.display_info.AddCacheUpdater(flags.TargetHttpProxiesCompleter)

  def Run(self, args):
    holder = base_classes.ComputeApiHolder(self.ReleaseTrack())
    client = holder.client

    target_http_proxy_refs = Delete.TARGET_HTTP_PROXY_ARG.ResolveAsResource(
        args,
        holder.resources,
        scope_lister=compute_flags.GetDefaultScopeLister(client))

    utils.PromptForDeletion(target_http_proxy_refs)

    requests = []
    for target_http_proxy_ref in target_http_proxy_refs:
      requests.append((client.apitools_client.targetHttpProxies, 'Delete',
                       client.messages.ComputeTargetHttpProxiesDeleteRequest(
                           **target_http_proxy_ref.AsDict())))

    return client.MakeRequests(requests)


@base.ReleaseTracks(base.ReleaseTrack.ALPHA)
class DeleteAlpha(Delete):
  """Delete target HTTP proxies.

  *{command}* deletes one or more target HTTP proxies.
  """

  TARGET_HTTP_PROXY_ARG = None

  @classmethod
  def Args(cls, parser):
    cls.TARGET_HTTP_PROXY_ARG = flags.TargetHttpProxyArgument(
        plural=True, include_alpha=True)
    cls.TARGET_HTTP_PROXY_ARG.AddArgument(parser, operation_type='delete')
    parser.display_info.AddCacheUpdater(flags.TargetHttpProxiesCompleterAlpha)

  def Run(self, args):
    holder = base_classes.ComputeApiHolder(self.ReleaseTrack())
    client = holder.client

    target_http_proxy_refs = self.TARGET_HTTP_PROXY_ARG.ResolveAsResource(
        args,
        holder.resources,
        scope_lister=compute_flags.GetDefaultScopeLister(client))

    utils.PromptForDeletion(target_http_proxy_refs)

    requests = []
    for target_http_proxy_ref in target_http_proxy_refs:
      if target_http_proxies_utils.IsRegionalTargetHttpProxiesRef(
          target_http_proxy_ref):
        requests.append(
            (client.apitools_client.regionTargetHttpProxies, 'Delete',
             client.messages.ComputeRegionTargetHttpProxiesDeleteRequest(
                 **target_http_proxy_ref.AsDict())))
      else:
        requests.append((client.apitools_client.targetHttpProxies, 'Delete',
                         client.messages.ComputeTargetHttpProxiesDeleteRequest(
                             **target_http_proxy_ref.AsDict())))

    return client.MakeRequests(requests)
