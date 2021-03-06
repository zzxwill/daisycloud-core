# Copyright 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
/Templates endpoint for Daisy v1 API
"""

from oslo_config import cfg
from oslo_log import log as logging
from webob.exc import HTTPBadRequest
from webob.exc import HTTPConflict
from webob.exc import HTTPForbidden
from webob.exc import HTTPNotFound
from webob import Response
import copy
import json

from daisy.api import policy
import daisy.api.v1
from daisy.api.v1 import controller
from daisy.api.v1 import filters
from daisy.common import exception
from daisy.common import property_utils
from daisy.common import utils
from daisy.common import wsgi
from daisy import i18n
from daisy import notifier
import daisy.registry.client.v1.api as registry
from daisy.registry.api.v1 import template

import daisy.api.backends.tecs.common as tecs_cmn
import daisy.api.backends.common as daisy_cmn
try:
    import simplejson as json
except ImportError:
    import json

daisy_tecs_path = tecs_cmn.daisy_tecs_path


LOG = logging.getLogger(__name__)
_ = i18n._
_LE = i18n._LE
_LI = i18n._LI
_LW = i18n._LW
SUPPORTED_PARAMS = template.SUPPORTED_PARAMS
SUPPORTED_FILTERS = template.SUPPORTED_FILTERS
ACTIVE_IMMUTABLE = daisy.api.v1.ACTIVE_IMMUTABLE
CONF = cfg.CONF
CONF.import_opt('disk_formats', 'daisy.common.config', group='image_format')
CONF.import_opt('container_formats', 'daisy.common.config',
                group='image_format')
CONF.import_opt('image_property_quota', 'daisy.common.config')

class Controller(controller.BaseController):
    """
    WSGI controller for Templates resource in Daisy v1 API

    The Templates resource API is a RESTful web Template for Template data. The API
    is as follows::

        GET  /Templates -- Returns a set of brief metadata about Templates
        GET  /Templates/detail -- Returns a set of detailed metadata about
                              Templates
        HEAD /Templates/<ID> -- Return metadata about an Template with id <ID>
        GET  /Templates/<ID> -- Return Template data for Template with id <ID>
        POST /Templates -- Store Template data and return metadata about the
                        newly-stored Template
        PUT  /Templates/<ID> -- Update Template metadata and/or upload Template
                            data for a previously-reserved Template
        DELETE /Templates/<ID> -- Delete the Template with id <ID>
    """

    def __init__(self):
        self.notifier = notifier.Notifier()
        registry.configure_registry_client()
        self.policy = policy.Enforcer()
        if property_utils.is_property_protection_enabled():
            self.prop_enforcer = property_utils.PropertyRules(self.policy)
        else:
            self.prop_enforcer = None

    def _enforce(self, req, action, target=None):
        """Authorize an action against our policies"""
        if target is None:
            target = {}
        try:
            self.policy.enforce(req.context, action, target)
        except exception.Forbidden:
            raise HTTPForbidden()

    def _get_filters(self, req):
        """
        Return a dictionary of query param filters from the request

        :param req: the Request object coming from the wsgi layer
        :retval a dict of key/value filters
        """
        query_filters = {}
        for param in req.params:
            if param in SUPPORTED_FILTERS:
                query_filters[param] = req.params.get(param)
                if not filters.validate(param, query_filters[param]):
                    raise HTTPBadRequest(_('Bad value passed to filter '
                                           '%(filter)s got %(val)s')
                                         % {'filter': param,
                                            'val': query_filters[param]})
        return query_filters

    def _get_query_params(self, req):
        """
        Extracts necessary query params from request.

        :param req: the WSGI Request object
        :retval dict of parameters that can be used by registry client
        """
        params = {'filters': self._get_filters(req)}

        for PARAM in SUPPORTED_PARAMS:
            if PARAM in req.params:
                params[PARAM] = req.params.get(PARAM)
        return params

    def _raise_404_if_cluster_deleted(self, req, cluster_id):
        cluster = self.get_cluster_meta_or_404(req, cluster_id)
        if cluster['deleted']:
            msg = _("Cluster with identifier %s has been deleted.") % cluster_id
            raise webob.exc.HTTPNotFound(msg)

    @utils.mutating
    def add_template(self, req, template):
        """
        Adds a new cluster template to Daisy.

        :param req: The WSGI/Webob Request object
        :param image_meta: Mapping of metadata about Template

        :raises HTTPBadRequest if x-Template-name is missing
        """
        self._enforce(req, 'add_template')
        template_name = template["name"]
        
        template = registry.add_template_metadata(req.context, template)

        return {'template': template}

    @utils.mutating
    def update_template(self, req, template_id, template):
        """
        Updates an existing Template with the registry.

        :param request: The WSGI/Webob Request object
        :param id: The opaque image identifier

        :retval Returns the updated image information as a mapping
        """
        self._enforce(req, 'update_template')
        try:
            template = registry.update_template_metadata(req.context,
                                                            template_id,
                                                            template)

        except exception.Invalid as e:
            msg = (_("Failed to update template metadata. Got error: %s") %
                   utils.exception_to_str(e))
            LOG.warn(msg)
            raise HTTPBadRequest(explanation=msg,
                                 request=req,
                                 content_type="text/plain")
        except exception.NotFound as e:
            msg = (_("Failed to find template to update: %s") %
                   utils.exception_to_str(e))
            LOG.warn(msg)
            raise HTTPNotFound(explanation=msg,
                               request=req,
                               content_type="text/plain")
        except exception.Forbidden as e:
            msg = (_("Forbidden to update template: %s") %
                   utils.exception_to_str(e))
            LOG.warn(msg)
            raise HTTPForbidden(explanation=msg,
                                request=req,
                                content_type="text/plain")
        except (exception.Conflict, exception.Duplicate) as e:
            LOG.warn(utils.exception_to_str(e))
            raise HTTPConflict(body=_('template operation conflicts'),
                               request=req,
                               content_type='text/plain')
        else:
            self.notifier.info('template.update', template)

        return {'template': template}
    @utils.mutating
    def delete_template(self, req, template_id):
        """
        delete a existing cluster template with the registry.

        :param request: The WSGI/Webob Request object
        :param id: The opaque image identifier

        :retval Returns the updated image information as a mapping
        """
        self._enforce(req, 'delete_template')
        try:
            registry.delete_template_metadata(req.context, template_id)
        except exception.NotFound as e:
            msg = (_("Failed to find template to delete: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPNotFound(explanation=msg,
                               request=req,
                               content_type="text/plain")
        except exception.Forbidden as e:
            msg = (_("Forbidden to delete template: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPForbidden(explanation=msg,
                                request=req,
                                content_type="text/plain")
        except exception.InUseByStore as e:
            msg = (_("template %(id)s could not be deleted because it is in use: "
                     "%(exc)s") % {"id": template_id, "exc": utils.exception_to_str(e)})
            LOG.error(msg)
            raise HTTPConflict(explanation=msg,
                               request=req,
                               content_type="text/plain")
        else:
            return Response(body='', status=200)
            
    def _del_general_params(self,param):
        del param['created_at']
        del param['updated_at']
        del param['deleted']
        del param['deleted_at']
        del param['id']
        
    def _del_cluster_params(self,cluster):
        del cluster['networks']
        del cluster['vlan_start']
        del cluster['vlan_end']
        del cluster['vni_start']
        del cluster['vni_end']
        del cluster['gre_id_start']
        del cluster['gre_id_end']
        del cluster['net_l23_provider']
        del cluster['public_vip']
        del cluster['segmentation_type']
        del cluster['base_mac']
        del cluster['name']
        
    @utils.mutating
    def export_db_to_json(self, req, template):
        """
        Template TECS to a cluster.
        :param req: The WSGI/Webob Request object
        :raises HTTPBadRequest if x-Template-cluster is missing
        """
        cluster_name = template.get('cluster_name',None)
        type = template.get('type',None)
        description = template.get('description',None)
        template_name = template.get('template_name',None)
        self._enforce(req, 'export_db_to_json')
        cinder_volume_list = []
        template_content = {}
        template_json = {}
        template_id = ""
        if not type or type == "tecs":
            try:
                params = {'filters': {'name':cluster_name}}
                clusters = registry.get_clusters_detail(req.context, **params)
                if clusters:
                    cluster_id = clusters[0]['id']
                else:
                    msg = "the cluster %s is not exist"%cluster_name
                    LOG.error(msg)
                    raise HTTPForbidden(explanation=msg, request=req, content_type="text/plain")
                
                params = {'filters': {'cluster_id':cluster_id}}            
                cluster = registry.get_cluster_metadata(req.context, cluster_id)
                roles = registry.get_roles_detail(req.context, **params)
                networks = registry.get_networks_detail(req.context, cluster_id,**params)
                for role in roles:
                    cinder_volume_params = {'filters': {'role_id':role['id']}} 
                    cinder_volumes = registry.list_cinder_volume_metadata(req.context, **cinder_volume_params)
                    for cinder_volume in cinder_volumes:
                        if cinder_volume.get('role_id',None):
                            cinder_volume['role_id'] = role['name']
                        self._del_general_params(cinder_volume)
                        cinder_volume_list.append(cinder_volume)
                    if role.get('config_set_id',None):
                        config_set = registry.get_config_set_metadata(req.context, role['config_set_id'])
                        role['config_set_id'] = config_set['name']
                    del role['cluster_id']
                    del role['status']
                    del role['progress']
                    del role['messages']
                    del role['config_set_update_progress']
                    self._del_general_params(role)
                for network in networks:
                    network_detail = registry.get_network_metadata(req.context, network['id'])
                    if network_detail.get('ip_ranges',None):
                        network['ip_ranges'] = network_detail['ip_ranges']
                    del network['cluster_id']
                    self._del_general_params(network)
                if cluster.get('routers',None):
                    for router in cluster['routers']:
                        del router['cluster_id']
                        self._del_general_params(router)
                if cluster.get('logic_networks',None):
                    for logic_network in cluster['logic_networks']:
                        for subnet in logic_network['subnets']:
                            del subnet['logic_network_id']
                            del subnet['router_id']
                            self._del_general_params(subnet)
                        del logic_network['cluster_id']
                        self._del_general_params(logic_network)
                if cluster.get('nodes',None):
                    del cluster['nodes']
                self._del_general_params(cluster)
                self._del_cluster_params(cluster)
                template_content['cluster'] = cluster
                template_content['roles'] = roles
                template_content['networks'] = networks
                template_content['cinder_volumes'] = cinder_volume_list
                template_json['content'] = json.dumps(template_content)
                template_json['type'] = 'tecs'
                template_json['name'] = template_name
                template_json['description'] = description
                
                template_host_params = {'cluster_name':cluster_name}
                template_hosts = registry.host_template_lists_metadata(req.context, **template_host_params)
                if template_hosts:
                    template_json['hosts'] = template_hosts[0]['hosts']
                else:
                    template_json['hosts'] = "[]"

                template_params = {'filters': {'name':template_name}}
                template_list = registry.template_lists_metadata(req.context, **template_params)
                if template_list:
                    update_template = registry.update_template_metadata(req.context, template_list[0]['id'], template_json)
                    template_id = template_list[0]['id']
                else:
                    add_template = registry.add_template_metadata(req.context, template_json)
                    template_id = add_template['id']
                    
                if template_id:
                    template_detail = registry.template_detail_metadata(req.context, template_id)
                    self._del_general_params(template_detail)
                    template_detail['content'] = json.loads(template_detail['content'])
                    if template_detail['hosts']:
                        template_detail['hosts'] = json.loads(template_detail['hosts'])
                    
                    tecs_json = daisy_tecs_path  + "%s.json"%template_name
                    cmd = 'rm -rf  %s' % (tecs_json,)
                    daisy_cmn.subprocess_call(cmd)
                    with open(tecs_json, "w+") as fp:
                        fp.write(json.dumps(template_detail))
            except exception.Invalid as e:
                raise HTTPBadRequest(explanation=e.msg, request=req)
            
        return {"template":template_detail}
        
    @utils.mutating
    def import_json_to_template(self, req, template):
        template_id = ""
        template = json.loads(template.get('template',None))
        template_cluster = copy.deepcopy(template)
        template_name = template_cluster.get('name',None)
        template_params = {'filters': {'name':template_name}}
        try:
            if template_cluster.get('content',None):
                template_cluster['content'] = json.dumps(template_cluster['content'])
            if template_cluster.get('hosts',None):
                template_cluster['hosts'] = json.dumps(template_cluster['hosts'])
            else:
                template_cluster['hosts'] = "[]" 
        
            template_list = registry.template_lists_metadata(req.context, **template_params)
            if template_list:
                update_template_cluster = registry.update_template_metadata(req.context, template_list[0]['id'], template_cluster)
                template_id = template_list[0]['id']
            else:
                add_template_cluster = registry.add_template_metadata(req.context, template_cluster)
                template_id = add_template_cluster['id']
                
            if template_id:
                template_detail = registry.template_detail_metadata(req.context, template_id)
                del template_detail['deleted']
                del template_detail['deleted_at']
        
        except exception.Invalid as e:
            raise HTTPBadRequest(explanation=e.msg, request=req)
       
        return {"template":template_detail}
        
    @utils.mutating
    def import_template_to_db(self, req, template):
        cluster_id = ""
        template_cluster = {}
        cluster_meta = {}
        template_meta = copy.deepcopy(template)
        template_name = template_meta.get('name',None)
        cluster_name = template_meta.get('cluster',None)
        template_params = {'filters': {'name':template_name}}
        template_list = registry.template_lists_metadata(req.context, **template_params)
        if template_list:
            template_cluster = template_list[0]
        else:
            msg = "the template %s is not exist" % template_name
            LOG.error(msg)
            raise HTTPForbidden(explanation=msg, request=req, content_type="text/plain")
        
        try:
            template_content = json.loads(template_cluster['content'])
            template_content_cluster = template_content['cluster']
            template_content_cluster['name'] = cluster_name
            template_content_cluster['networking_parameters'] = str(template_content_cluster['networking_parameters'])
            template_content_cluster['logic_networks'] = str(template_content_cluster['logic_networks'])
            template_content_cluster['logic_networks'] = template_content_cluster['logic_networks'].replace("\'true\'","True")
            template_content_cluster['routers'] = str(template_content_cluster['routers'])
            
            if template_cluster['hosts']:
                template_hosts = json.loads(template_cluster['hosts'])
                template_host_params = {'cluster_name':cluster_name}
                template_host_list = registry.host_template_lists_metadata(req.context, **template_host_params)
                if template_host_list:
                    update_template_meta = {"cluster_name": cluster_name, "hosts":json.dumps(template_hosts)}
                    registry.update_host_template_metadata(req.context, template_host_list[0]['id'], update_template_meta)
                else:
                    template_meta = {"cluster_name": cluster_name, "hosts":json.dumps(template_hosts)}
                    registry.add_host_template_metadata(req.context, template_meta)
            
            cluster_params = {'filters': {'name':cluster_name}}
            clusters = registry.get_clusters_detail(req.context, **cluster_params)
            if clusters:
                msg = "the cluster %s is exist" % clusters[0]['name']
                LOG.error(msg)
                raise HTTPForbidden(explanation=msg, request=req, content_type="text/plain")
            else:
                cluster_meta = registry.add_cluster_metadata(req.context, template_content['cluster'])
                cluster_id = cluster_meta['id']
            
            params = {'filters':{}}
            networks = registry.get_networks_detail(req.context, cluster_id,**params)
            template_content_networks = template_content['networks']
            for template_content_network in template_content_networks:
                template_content_network['ip_ranges'] = str(template_content_network['ip_ranges'])
                network_exist = 'false'
                for network in networks:
                    if template_content_network['name'] == network['name']:
                        update_network_meta = registry.update_network_metadata(req.context, network['id'], template_content_network)
                        network_exist = 'true'

                if network_exist == 'false':
                    template_content_network['cluster_id'] = cluster_id
                    add_network_meta = registry.add_network_metadata(req.context, template_content_network)
                    
            params = {'filters': {'cluster_id':cluster_id}}
            roles = registry.get_roles_detail(req.context, **params)
            template_content_roles = template_content['roles']
            for template_content_role in template_content_roles:
                role_exist = 'false'
                del template_content_role['config_set_id']
                for role in roles:
                    if template_content_role['name'] == role['name']:
                        update_role_meta = registry.update_role_metadata(req.context, role['id'], template_content_role)
                        role_exist = 'true'
                
                if role_exist == 'false':
                    template_content_role['cluster_id'] = cluster_id
                    add_role_meta = registry.add_role_metadata(req.context, template_content_role)
                    
            cinder_volumes = registry.list_cinder_volume_metadata(req.context, **params)
            template_content_cinder_volumes = template_content['cinder_volumes']
            for template_content_cinder_volume in template_content_cinder_volumes:
                cinder_volume_exist = 'false'
                roles = registry.get_roles_detail(req.context, **params)
                for role in roles:
                    if template_content_cinder_volume['role_id'] == role['name']:
                        template_content_cinder_volume['role_id'] = role['id']
                
                for cinder_volume in cinder_volumes:
                    if template_content_cinder_volume['role_id'] == cinder_volume['role_id']:
                        update_cinder_volume_meta = registry.update_cinder_volume_metadata(req.context, cinder_volume['id'], template_content_cinder_volume)
                        cinder_volume_exist = 'true'
                        
                if cinder_volume_exist == 'false':
                    add_cinder_volumes = registry.add_cinder_volume_metadata(req.context, template_content_cinder_volume)
        
        except exception.Invalid as e:
            raise HTTPBadRequest(explanation=e.msg, request=req)
        return {"template":cluster_meta}
            
    @utils.mutating
    def get_template_detail(self, req, template_id):
        """
        delete a existing cluster template with the registry.
        :param request: The WSGI/Webob Request object
        :param id: The opaque image identifie
        :retval Returns the updated image information as a mapping
        """
        self._enforce(req, 'get_template_detail')
        try:
            template = registry.template_detail_metadata(req.context, template_id)
            return {'template': template}
        except exception.NotFound as e:
            msg = (_("Failed to find template: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPNotFound(explanation=msg,
                               request=req,
                               content_type="text/plain")
        except exception.Forbidden as e:
            msg = (_("Forbidden to get template: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPForbidden(explanation=msg,
                                request=req,
                                content_type="text/plain")
        except exception.InUseByStore as e:
            msg = (_("template %(id)s could not be get because it is in use: "
                     "%(exc)s") % {"id": template_id, "exc": utils.exception_to_str(e)})
            LOG.error(msg)
            raise HTTPConflict(explanation=msg,
                               request=req,
                               content_type="text/plain")
        else:
            return Response(body='', status=200)
    
    @utils.mutating
    def get_template_lists(self, req):
        self._enforce(req, 'get_template_lists')
        params = self._get_query_params(req)
        try:
            template_lists = registry.template_lists_metadata(req.context, **params)
        except exception.Invalid as e:
            raise HTTPBadRequest(explanation=e.msg, request=req)
        return dict(template=template_lists)
        
class TemplateDeserializer(wsgi.JSONRequestDeserializer):
    """Handles deserialization of specific controller method requests."""
        
    def _deserialize(self, request):
        result = {}
        result["template"] = utils.get_template_meta(request)
        return result
        
    def add_template(self, request):
        return self._deserialize(request)
        
    def update_template(self, request):
        return self._deserialize(request)
        
    def export_db_to_json(self, request):
        return self._deserialize(request)
        
    def import_json_to_template(self, request):
        return self._deserialize(request)
        
    def import_template_to_db(self, request):
        return self._deserialize(request)

class TemplateSerializer(wsgi.JSONResponseSerializer):
    """Handles serialization of specific controller method responses."""
        
    def __init__(self):
        self.notifier = notifier.Notifier()
        
    def add_template(self, response, result):
        template = result['template']
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(template=template))
        return response
        
    def delete_template(self, response, result):
        template = result['template']
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(template=template))
        return response
    def get_template_detail(self, response, result):
        template = result['template']
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(template=template))
        return response
    def update_template(self, response, result):
        template = result['template']
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(template=template))
        return response        
        
    def export_db_to_json(self, response, result):
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(result)
        return response
        
    def import_json_to_template(self, response, result):
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(result)
        return response
        
    def import_template_to_db(self, response, result):
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(result)
        return response
        
def create_resource():
    """Templates resource factory method"""
    deserializer = TemplateDeserializer()
    serializer = TemplateSerializer()
    return wsgi.Resource(Controller(), deserializer, serializer)
