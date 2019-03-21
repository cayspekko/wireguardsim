#!/usr/bin/env -S pipenv run python3
import subprocess
import itertools
import yaml
import os
from functools import partial
from tempfile import NamedTemporaryFile
    

def _ip_netns_exec(nsname, cmd):
    return f"ip netns exec {nsname} {cmd}"


def ip_netns_exec(nsname, cmds):
    for cmd in cmds:
        yield _ip_netns_exec(nsname, cmd)


def build_namespace(name):
    yield f"ip netns add {name}"
        

def build_veth(source, destination):
    """
    :param source: tuple (name, port)
    :param destination:  (name, port)
    :return:
    """
    source, src_port = source
    destination, dst_port = destination
    src_port = src_port or 0
    dst_port = dst_port or 0
    yield f"ip link add veth{source}{src_port or 0} type veth peer name veth{destination}{dst_port or 0}"
    yield f"ip link set veth{source}{src_port or 0} netns {source}"
    yield f"ip link set veth{destination}{dst_port or 0} netns {destination}"
    yield _ip_netns_exec(source, f"ip link set veth{source}{src_port} up")
    yield _ip_netns_exec(destination, f"ip link set veth{destination}{dst_port} up")


def add_bridge(bridge_name):
    yield f"ip link add {bridge_name} type bridge"
  

def bridge_veth(veth_name, bridge_name):
    yield f"ip link set veth{veth_name} master {bridge_name}"
    

def veth_up(veth_name):
    yield f"ip link set {veth_name} up"


def add_ip(veth_name, ip):
    yield f"ip addr add {ip} dev {veth_name}"


def add_route(network, next_hop):
    yield f"ip route add {network} via {next_hop}"


def masquerade(veth_name):
    yield f"iptables -t nat -A POSTROUTING -o {veth_name} -j MASQUERADE"


def forward_ip4():
    yield f"sysctl -w net.ipv4.ip_forward=1"


def clean_ns(nsname):
    yield f"ip netns del {nsname}"


class DHCPServer:
    config = """
start {start}
end	{end}
interface {interface}
lease_file {lease_file}
pid_file {pid_file}
"""

    cmd = "ip netns exec {namespace} udhcpd -f {config}"

    def __init__(self, start, end, interface, namespace):
        self.p = None
        self.files = [NamedTemporaryFile('w+') for _ in range(3)]
        self.config = self.config.format(start=start, end=end, interface=interface, lease_file=self.files[1].name, pid_file=self.files[2].name)
        self.cmd = self.cmd.format(namespace=namespace, config=self.files[0].name)
        self.files[0].write(self.config)

    def start(self):

        self.p = subprocess.Popen(self.cmd.split())

    def stop(self):
        if self.p:
            self.p.terminate()
            self.p.wait(5)
            self.p = None


class BaseNode:
    def __init__(self, name):
        self.name = name

    def build_namespace(self):
        return itertools.chain(
            build_namespace(self.name),
            # ip_netns_exec(self.name, forward_ip4())
        )

    def configure_veths(self, name, port):
        return iter([])

    def configure_servers(self, name):
        return iter([])

    def configure_ip(self, port, ip_address, gateway, name=None):
        name = name or f'veth{self.name}{port}'
        return itertools.chain(
            ip_netns_exec(self.name, add_ip(f'{name}', ip_address)),
            ip_netns_exec(self.name, add_route('default', gateway)) if gateway else iter([])
        )

    def configure_extra(self, node):
        return iter([])


class Router(BaseNode):
    def build_namespace(self):
        return itertools.chain(
            super().build_namespace(),
            ip_netns_exec(self.name, add_bridge("br" + self.name)),
            ip_netns_exec(self.name, veth_up("br" + self.name))
        )

    def configure_veths(self, name, port):
        if name != self.name:
            return super().configure_veths(name, port)

        port = port or 0
        if port == 0:
            return super().configure_veths(name, port)

        return itertools.chain(
            super().configure_veths(name, port),
            ip_netns_exec(self.name, bridge_veth(f"{self.name}{port}", "br" + self.name))
        )

    def configure_ip(self, port, ip_address, gateway, name=None):
        if port == 0:
            return super().configure_ip(port, ip_address, gateway)
        else:
            return super().configure_ip(port, ip_address, gateway, name=f'br{self.name}')

    def configure_extra(self, node):
        name = node['name']
        ports = node['ports']
        return itertools.chain(
            super().configure_extra(node),
            ip_netns_exec(name, *(masquerade(f'veth{name}{i}') for i, port in enumerate(ports) if port.get('masquerade'))),
        )


type_map = {
    'router': Router,
}


def generate_script(script, *generators):
    for generator in generators:
        for cmd in generator:
            script.append(cmd)


def main(argv):
    # First, load the topology from file
    with open(argv[1]) as f:
        topology = yaml.load(f, Loader=yaml.Loader)

    # initialize each node object
    for node in topology['nodes']:
        node_init = type_map.get(node['type'], BaseNode)(node['name'])
        node['init'] = node_init

    # Generate the script to run
    script = []
    gen = partial(generate_script, script)

    gen(
        # create the namespaces
        *(node['init'].build_namespace() for node in topology['nodes']),

        # create the veths
        *(build_veth(
            (link['source']['name'], link['source'].get('port')),
            (link['destination']['name'], link['destination'].get('port'))
        ) for link in topology['links']),

        # configure veths
        *(node['init'].configure_veths(link['source']['name'], link['source'].get('port'))
          for link in topology['links'] for node in topology['nodes']),
        *(node['init'].configure_veths(link['destination']['name'], link['destination'].get('port'))
          for link in topology['links'] for node in topology['nodes']),

        *(node['init'].configure_ip(i, port['ip_address'], port.get('gateway'))
          for node in topology['nodes'] for i, port in enumerate(node['ports'])),

        *(node['init'].configure_extra(node) for node in topology['nodes'])
    )

    cleanup_script = []
    gen = partial(generate_script, cleanup_script)
    gen(
        *(clean_ns(node['name']) for node in topology['nodes'])
    )

    def write_script(filename, script):
        with open(filename, 'w') as f:
            f.write('#!/bin/bash\n')
            f.write("\n".join(l for l in script))
        os.chmod(filename, 0o774)

    script_filename = f'{argv[1]}.sh'.replace('.yaml', '')
    cleanup_script_filename = f'cleanup_{argv[1]}.sh'.replace('.yaml', '')

    write_script(script_filename, script)
    write_script(cleanup_script_filename, cleanup_script)

    print('#!/bin/bash')
    for l in script:
        print(l)


if __name__ == "__main__":
    import sys
    main(sys.argv)
