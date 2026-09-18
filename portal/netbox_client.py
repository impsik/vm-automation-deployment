"""Small NetBox 4.x client. Only updates records owned by this portal request."""
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request


class NetBoxError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the API credential to a redirect target.
        return None


class NetBoxClient:
    def __init__(self, url, token, cluster_id, timeout=5):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise NetBoxError('Configure a valid NetBox base URL without credentials, query or fragment')
        if not token or not cluster_id:
            raise NetBoxError('NetBox API token and cluster ID are required')
        self.url = url.rstrip('/')
        self.token = token
        self.cluster_id = int(cluster_id)
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def api(self, method, path, payload=None, **query):
        url = self.url + '/api/' + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        authorization = ('Bearer ' if self.token.startswith('nbt_') else 'Token ') + self.token
        request = urllib.request.Request(url, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': authorization, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            # Do not expose server response bodies, credentials or infrastructure details.
            raise NetBoxError(f'NetBox API returned HTTP {error.code} for {method} {path}') from None
        except (OSError, ValueError, urllib.error.URLError):
            raise NetBoxError('NetBox is unreachable or returned an invalid response') from None

    def lookup(self, path, **query):
        result = self.api('GET', path, limit=2, **query)
        if not isinstance(result, dict) or 'count' not in result or not isinstance(result.get('results'), list):
            raise NetBoxError('Invalid NetBox list response')
        if result['count'] > 1:
            raise NetBoxError('Ambiguous existing NetBox records; administrator review required')
        return result['results'][0] if result['results'] else None

    def register(self, request, additional_disks):
        marker = f'VM Foundry request: {request["id"]}'
        name = request.get('fqdn') or request['hostname']
        existing = self.lookup('virtualization/virtual-machines/', name=name, cluster_id=self.cluster_id)
        if existing and marker not in existing.get('comments', '').splitlines():
            raise NetBoxError('A VM with this name already exists in NetBox and is not owned by this request')
        resources = request['resources']
        payload = dict(name=name, cluster=self.cluster_id, status='active',
            vcpus=resources['vcpu'], memory=resources['memory_gb'] * 1024,
            disk=(resources['disk_gb'] + sum(d['size_gb'] for d in additional_disks)) * 1024,
            description=str(request.get('purpose') or '')[:200],
            comments='\n'.join([marker, f'Owner: {request.get("owner", "")}',
                f'Project: {request.get("project", "")}', f'Environment: {request.get("environment", "")}',
                f'Image: {request.get("image", "")}', f'Network: {request.get("network", "")}']))
        path = 'virtualization/virtual-machines/'
        vm = self.api('PATCH', f'{path}{existing["id"]}/', payload) if existing else self.api('POST', path, payload)
        result = {'vm_id': vm['id'], 'url': f'{self.url}/virtualization/virtual-machines/{vm["id"]}/'}
        if request.get('ip_address'):
            ip = ipaddress.ip_address(request['ip_address'])
            # The portal only knows the lease address, not its authoritative prefix.
            address = f'{ip}/{ip.max_prefixlen}'
            interface = self.lookup('virtualization/interfaces/', virtual_machine_id=vm['id'], name='primary')
            if not interface:
                interface = self.api('POST', 'virtualization/interfaces/',
                                     {'virtual_machine': vm['id'], 'name': 'primary', 'enabled': True,
                                      'description': marker})
            existing_ip = self.lookup('ipam/ip-addresses/', address=str(ip))
            if existing_ip and (existing_ip.get('assigned_object_type') != 'virtualization.vminterface'
                                or existing_ip.get('assigned_object_id') != interface['id']):
                raise NetBoxError('IP address already exists in NetBox; refusing to reassign it')
            if not existing_ip:
                existing_ip = self.api('POST', 'ipam/ip-addresses/', dict(address=address, status='active',
                    assigned_object_type='virtualization.vminterface', assigned_object_id=interface['id'],
                    dns_name=name, description=marker))
            self.api('PATCH', f'{path}{vm["id"]}/', {f'primary_ip{ip.version}': existing_ip['id']})
            result['ip_address'] = str(ip)
        return result
