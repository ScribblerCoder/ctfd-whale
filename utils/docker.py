import json
import random
import uuid
from collections import OrderedDict
from datetime import datetime
import re

import docker
from flask import current_app

from CTFd.utils import get_config

from .cache import CacheProvider
from .exceptions import WhaleError


def get_docker_client():
    if get_config("whale:docker_use_ssl", False):
        tls_config = docker.tls.TLSConfig(
            verify=True,
            ca_cert=get_config("whale:docker_ssl_ca_cert") or None,
            client_cert=(
                get_config("whale:docker_ssl_client_cert"),
                get_config("whale:docker_ssl_client_key")
            ),
        )
        return docker.DockerClient(
            base_url=get_config("whale:docker_api_url"),
            tls=tls_config,
        )
    else:
        return docker.DockerClient(base_url=get_config("whale:docker_api_url"))


class DockerUtils:
    @staticmethod
    def init():
        try:
            DockerUtils.client = get_docker_client()
            # docker-py is thread safe: https://github.com/docker/docker-py/issues/619
        except Exception:
            raise WhaleError(
                'Docker Connection Error\n'
                'Please ensure the docker api url (first config item) is correct\n'
                'if you are using unix:///var/run/docker.sock, check if the socket is correctly mapped'
            )
        credentials = get_config("whale:docker_credentials")
        if credentials and credentials.count(':') >= 1:
            try:
                DockerUtils.client.login(*credentials.split(':'))
            except Exception:
                raise WhaleError('docker.io failed to login, check your credentials')

    @staticmethod
    def get_images_by_prefix(prefix, force_refresh=False):
        """
        Get all Docker images that start with the specified prefix
        
        Args:
            prefix (str): The prefix to filter images by
            force_refresh (bool): Whether to force refresh the image list
            
        Returns:
            list: List of image dictionaries with name, tags, size, created, and id
        """
        try:
            client = get_docker_client()
            
            # Get all images
            all_images = client.images.list()
            filtered_images = []
            
            for image in all_images:
                # Each image can have multiple tags
                for tag in image.tags:
                    if tag.startswith(prefix):
                        # Parse the image information
                        image_info = {
                            'name': tag,
                            'short_name': tag.replace(prefix, '').lstrip('/'),
                            'id': image.short_id,
                            'size': DockerUtils._format_size(image.attrs.get('Size', 0)),
                            'created': DockerUtils._format_datetime(image.attrs.get('Created', '')),
                            'created_timestamp': image.attrs.get('Created', ''),
                            'labels': image.attrs.get('Config', {}).get('Labels') or {},
                            'architecture': image.attrs.get('Architecture', 'unknown'),
                        }
                        
                        # Try to get additional metadata
                        try:
                            # Get image history for more details
                            history = image.history()
                            if history:
                                image_info['layers'] = len(history)
                        except:
                            image_info['layers'] = 'unknown'
                        
                        filtered_images.append(image_info)
            
            # Sort by creation time (newest first)
            filtered_images.sort(key=lambda x: x.get('created_timestamp', ''), reverse=True)
            
            return filtered_images
            
        except Exception as e:
            raise Exception(f"Failed to fetch Docker images: {str(e)}")

    @staticmethod
    def _format_size(size_bytes):
        """Format size in bytes to human readable format"""
        if size_bytes == 0:
            return "0 B"
        
        size_names = ["B", "KB", "MB", "GB", "TB"]
        import math
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_names[i]}"

    @staticmethod
    def _format_datetime(datetime_str):
        """Format ISO datetime string to readable format"""
        if not datetime_str:
            return "Unknown"
        
        try:
            # Parse the ISO format datetime
            dt = datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
            return dt.strftime('%Y-%m-%d %H:%M:%S UTC')
        except:
            return datetime_str

    @staticmethod
    def pull_image(image_name):
        """
        Pull a Docker image
        
        Args:
            image_name (str): Name of the image to pull
            
        Returns:
            tuple: (success, message)
        """
        try:
            client = get_docker_client()
            
            # Pull the image
            client.images.pull(image_name)
            return True, f"Successfully pulled image: {image_name}"
            
        except Exception as e:
            return False, f"Failed to pull image {image_name}: {str(e)}"

    @staticmethod
    def remove_image(image_name, force=False):
        """
        Remove a Docker image
        
        Args:
            image_name (str): Name of the image to remove
            force (bool): Force removal
            
        Returns:
            tuple: (success, message)
        """
        try:
            client = get_docker_client()
            
            # Remove the image
            client.images.remove(image_name, force=force)
            return True, f"Successfully removed image: {image_name}"
            
        except Exception as e:
            return False, f"Failed to remove image {image_name}: {str(e)}"

    @staticmethod
    def get_image_details(image_name):
        """
        Get detailed information about a specific image
        
        Args:
            image_name (str): Name of the image
            
        Returns:
            dict: Detailed image information
        """
        try:
            client = get_docker_client()
            image = client.images.get(image_name)
            
            return {
                'name': image_name,
                'id': image.id,
                'short_id': image.short_id,
                'tags': image.tags,
                'size': DockerUtils._format_size(image.attrs.get('Size', 0)),
                'created': DockerUtils._format_datetime(image.attrs.get('Created', '')),
                'architecture': image.attrs.get('Architecture', 'unknown'),
                'os': image.attrs.get('Os', 'unknown'),
                'config': image.attrs.get('Config', {}),
                'labels': image.attrs.get('Config', {}).get('Labels') or {},
                'env': image.attrs.get('Config', {}).get('Env') or [],
                'exposed_ports': list((image.attrs.get('Config', {}).get('ExposedPorts') or {}).keys()),
                'working_dir': image.attrs.get('Config', {}).get('WorkingDir', ''),
                'entrypoint': image.attrs.get('Config', {}).get('Entrypoint') or [],
                'cmd': image.attrs.get('Config', {}).get('Cmd') or [],
            }
            
        except Exception as e:
            raise Exception(f"Failed to get image details for {image_name}: {str(e)}")

    @staticmethod
    def add_container(container):
        if container.challenge.docker_image.startswith("{"):
            DockerUtils._create_grouped_container(DockerUtils.client, container)
        else:
            DockerUtils._create_standalone_container(DockerUtils.client, container)

    @staticmethod
    def _create_standalone_container(client, container):
        dns = get_config("whale:docker_dns", "").split(",")
        node = DockerUtils.choose_node(
            container.challenge.docker_image,
            get_config("whale:docker_swarm_nodes", "").split(",")
        )
        credentials = get_config("whale:docker_credentials")
        if credentials and credentials.count(':') >= 1:
            try:
                image = client.images.get(container.challenge.docker_image)
                print(f"image {image} found!")
            except docker.errors.ImageNotFound:
                print(f"image not found, pulling...")
                client.images.pull(container.challenge.docker_image)
                print(f"pulling image {container.challenge.docker_image}")
            except docker.errors.APIError:
                print("registry login issues.. retrying to login")
                credentials = get_config("whale:docker_credentials")
                client.login(*credentials.split(':'))
                print(f"pulling image {container.challenge.docker_image}")
                client.images.pull(container.challenge.docker_image)

        client.services.create(
            image=container.challenge.docker_image,
            name=f'{container.user_id}-{container.uuid}',
            env={'FLAG': container.flag}, dns_config=docker.types.DNSConfig(nameservers=dns),
            networks=[get_config("whale:docker_auto_connect_network", "ctfd_frp-containers")],
            resources=docker.types.Resources(
                mem_limit=DockerUtils.convert_readable_text(
                    container.challenge.memory_limit),
                cpu_limit=int(container.challenge.cpu_limit * 1e9)
            ),
            labels={
                'whale_id': f'{container.user_id}-{container.uuid}'
            },  # for container deletion
            constraints=['node.labels.name==' + node],
            endpoint_spec=docker.types.EndpointSpec(mode='dnsrr', ports={})
        )

    @staticmethod
    def _create_grouped_container(client, container):
        range_prefix = CacheProvider(app=current_app).get_available_network_range()

        ipam_pool = docker.types.IPAMPool(subnet=range_prefix)
        ipam_config = docker.types.IPAMConfig(
            driver='default', pool_configs=[ipam_pool])
        network_name = f'{container.user_id}-{container.uuid}'
        network = client.networks.create(
            network_name, internal=True,
            ipam=ipam_config, attachable=True,
            labels={'prefix': range_prefix},
            driver="overlay", scope="swarm"
        )

        dns = []
        containers = get_config("whale:docker_auto_connect_containers", "").split(",")
        for c in containers:
            if not c:
                continue
            network.connect(c)
            if "dns" in c:
                network.reload()
                for name in network.attrs['Containers']:
                    if network.attrs['Containers'][name]['Name'] == c:
                        dns.append(network.attrs['Containers'][name]['IPv4Address'].split('/')[0])

        has_processed_main = False
        try:
            images = json.loads(
                container.challenge.docker_image,
                object_pairs_hook=OrderedDict
            )
        except json.JSONDecodeError:
            raise WhaleError(
                "Challenge Image Parse Error\n"
                "plase check the challenge image string"
            )
        for name, config in images.items():
            # Handle both nested and flat formats
            if isinstance(config, dict):
                image = config.get('image')
                extra_cap = config.get('extra_cap', [])
                include_flag = config.get('flag', True)  # Default to True for backward compatibility
            else:
                image = config
                extra_cap = []
                include_flag = True
            
            if has_processed_main:
                container_name = f'{container.user_id}-{uuid.uuid4()}'
            else:
                container_name = f'{container.user_id}-{container.uuid}'
                node = DockerUtils.choose_node(image, get_config("whale:docker_swarm_nodes", "").split(","))
                has_processed_main = True
            
            # Build environment variables
            env = {}
            if include_flag:
                env['FLAG'] = container.flag
            
            # Build container capabilities
            cap_add = extra_cap if extra_cap else []
            
            client.services.create(
                image=image, 
                name=container_name, 
                networks=[
                    docker.types.NetworkAttachmentConfig(network_name, aliases=[name])
                ],
                env=env,
                dns_config=docker.types.DNSConfig(nameservers=dns),
                resources=docker.types.Resources(
                    mem_limit=DockerUtils.convert_readable_text(
                        container.challenge.memory_limit
                    ),
                    cpu_limit=int(container.challenge.cpu_limit * 1e9)
                ),
                labels={
                    'whale_id': f'{container.user_id}-{container.uuid}'
                },  # for container deletion
                hostname=name, 
                constraints=['node.labels.name==' + node],
                endpoint_spec=docker.types.EndpointSpec(mode='dnsrr', ports={}),
                cap_add=cap_add
            )

    @staticmethod
    def remove_container(container):
        whale_id = f'{container.user_id}-{container.uuid}'

        for s in DockerUtils.client.services.list(filters={'label': f'whale_id={whale_id}'}):
            s.remove()

        networks = DockerUtils.client.networks.list(names=[whale_id])
        if len(networks) > 0:  # is grouped containers
            auto_containers = get_config("whale:docker_auto_connect_containers", "").split(",")
            redis_util = CacheProvider(app=current_app)
            for network in networks:
                for container in auto_containers:
                    try:
                        network.disconnect(container, force=True)
                    except Exception:
                        pass
                redis_util.add_available_network_range(network.attrs['Labels']['prefix'])
                network.remove()

    @staticmethod
    def convert_readable_text(text):
        lower_text = text.lower()

        if lower_text.endswith("k"):
            return int(text[:-1]) * 1024

        if lower_text.endswith("m"):
            return int(text[:-1]) * 1024 * 1024

        if lower_text.endswith("g"):
            return int(text[:-1]) * 1024 * 1024 * 1024

        return 0

    @staticmethod
    def choose_node(image, nodes):
        win_nodes = []
        linux_nodes = []
        for node in nodes:
            if node.startswith("windows"):
                win_nodes.append(node)
            else:
                linux_nodes.append(node)
        try:
            tag = image.split(":")[1:]
            if len(tag) and tag[0].startswith("windows"):
                return random.choice(win_nodes)
            return random.choice(linux_nodes)
        except IndexError:
            raise WhaleError(
                'No Suitable Nodes.\n'
                'If you are using Whale for the first time, \n'
                'Please Setup Swarm Nodes Correctly and Lable Them with\n'
                'docker node update --label-add "name=linux-1" $(docker node ls -q)'
            )