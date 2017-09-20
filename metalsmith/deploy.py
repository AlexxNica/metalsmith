# Copyright 2015 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from oslo_utils import excutils

from metalsmith import os_api


LOG = logging.getLogger(__name__)


def _log_node(node):
    if node.name:
        return '%s (UUID %s)' % (node.name, node.uuid)
    else:
        return node.uuid


def _get_capabilities(node):
    return dict(x.split(':', 1) for x in
                node.properties.get('capabilities', '').split(',') if x)


def reserve(api, nodes, profile):
    suitable_nodes = []
    for node in nodes:
        caps = _get_capabilities(node)
        LOG.debug('Capabilities for node %(node)s: %(cap)s',
                  {'node': _log_node(node), 'cap': caps})
        if caps.get('profile') == profile:
            suitable_nodes.append(node)

    if not suitable_nodes:
        raise RuntimeError('No nodes found with profile %s' % profile)

    for node in suitable_nodes:
        try:
            api.validate_node(node.uuid)
        except RuntimeError as exc:
            LOG.warn('Node %(node)s failed validation: %(err)s',
                     {'node': _log_node(node), 'err': exc})
            continue

        if not node.properties.get('local_gb'):
            LOG.warn('No local_gb for node %s', _log_node(node))
            continue

        try:
            return api.update_node(node.uuid, instance_uuid=node.uuid)
        except os_api.ir_exc.Conflict:
            LOG.info('Node %s was occupied, proceeding with the next',
                     _log_node(node))

    raise RuntimeError('Unable to reserve any node')


def clean_up(api, node, instance_info):
    try:
        api.update_node(node.uuid, instance_uuid=os_api.REMOVE)
    except Exception:
        LOG.debug('Failed to remove instance_uuid, assuming already removed')

    for port in instance_info.get('ports', ()):
        api.delete_port(port.id)

    for node_port in instance_info.get('node_ports', ()):
        try:
            api.update_node_port(node_port.uuid,
                                 {'/extra/vif_port_id': os_api.REMOVE})
        except Exception:
            LOG.debug('Failed to remove VIF id from %s, assuming removed',
                      node_port.uuid)


def provision(api, node, network, image, instance_info):
    updates = {'/instance_info/ramdisk': image.properties['ramdisk_id'],
               '/instance_info/kernel': image.properties['kernel_id'],
               '/instance_info/image_source': image.id,
               '/instance_info/root_gb': node.properties['local_gb']}
    node = api.update_node(node.uuid, updates)

    node_ports = api.list_node_ports(node.uuid)
    for node_port in node_ports:
        port = api.create_port(mac_address=node_port.address,
                               network_id=network.id)
        LOG.debug('Created Neutron port %s', port)
        instance_info.setdefault('ports', []).append(port)

        api.update_node_port(node_port.uuid,
                             {'/extra/vif_port_id': port.id})
        instance_info.setdefault('node_ports', []).append(node_port)
        LOG.debug('Ironic port %(node_port)s (%(mac)s) associated with '
                  'Neutron port %(port)s',
                  {'node_port': node_port.uuid,
                   'mac': node_port.address,
                   'port': port.id})

    api.validate_node(node.uuid, validate_deploy=True)

    api.node_action(node.uuid, 'active')


def deploy(profile, image_id, network_id, auth_args):
    """Deploy an image on a given profile."""
    LOG.debug('Deploying image %(image)s on node with profile %(profile)s '
              'on network %(net)s',
              {'image': image_id, 'profile': profile, 'net': network_id})
    api = os_api.API(**auth_args)

    image = api.get_image_info(image_id)
    if image is None:
        raise RuntimeError('Image %s does not exist' % image_id)
    for im_prop in ('kernel_id', 'ramdisk_id'):
        if not image.properties.get(im_prop):
            raise RuntimeError('%s property is required on image' % im_prop)
    LOG.debug('Image: %s', image)

    network = api.get_network(network_id)
    if network is None:
        raise RuntimeError('Network %s does not exist' % network_id)
    LOG.debug('Network: %s', network)

    nodes = api.list_nodes()
    LOG.debug('Ironic nodes: %s', nodes)
    if not len(nodes):
        raise RuntimeError('No available nodes found')
    LOG.info('Got list of %d available nodes from Ironic', len(nodes))

    node = reserve(api, nodes, profile)
    LOG.info('Reserved node %s', _log_node(node))

    instance_info = {}
    try:
        provision(api, node, network, image, instance_info)
    except Exception:
        with excutils.save_and_reraise_exception():
            LOG.error('Deploy failed, cleaning up')
            try:
                clean_up(api, node, instance_info)
            except Exception:
                LOG.exception('Clean up also failed')