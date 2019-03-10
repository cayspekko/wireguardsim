import subprocess
import itertools
import ipaddress
import yaml
import functools
    

def run(cmd):
    return subprocess.check_output(cmd.split())


def sudo(cmd):
    return "sudo " + cmd


def ip_netns_exec(nsname, cmds):
    for cmd in cmds:
        yield f"ip netns exec {nsname} {cmd}"


def build_namespace(name):
    yield f"ip netns add {name}"
        

def build_veth(source, destination):
    """
    :param source: tuple (name, port)
    :param destination:  (name, port)
    :return:
    """
    source, src_port = source
    destination, destination_port = destination
    yield f"ip link add veth{source}{src_port or 0} type veth peer name veth{destination}{destination_port or 0}"
    yield f"ip link set veth{source}{src_port or 0} netsn {source}"
    yield f"ip link set veth{destination}{destination_port or 0} netns {destination}"
  

def add_bridge(bridge_name):
    yield f"ip link add {bridge_name} type bridge"
  

def bridge_veth(veth_name, bridge_name):
    yield f"ip link set {veth_name} master {bridge_name}"
    

def veth_up(veth_name):
    yield f"ip link set {veth_name} up"


def add_ip(veth_name, ip):
    yield f"ip addr add {ip} dev {veth_name}"


def add_route(network, next_hop):
    yield f"ip route add {network} via {next_hop}"


def masquerade(veth_name):
    yield f"iptables -o {veth_name} -j MASQUERADE"


def next_network(network, mask_inc):
    n = ipaddress.ip_network(network)
    while True:
        rval = str(n)
        n = ipaddress.ip_network('{}/{}'.format(n.network_address + (1 << (n.max_prefixlen - mask_inc)), n.netmask))
        yield rval


class BaseNode:
    def __init__(self, name):
        self.name = name

    def build_namespace(self):
        return build_namespace(self.name)


class Router(BaseNode):
    def build_namespace(self):
        return itertools.chain(
            super().build_namespace(),
            ip_netns_exec(self.name, add_bridge(self.name))
        )


class Cloud(BaseNode):
    def __init__(self, name):
        self.next_network = next_network("30.0.0.0/30", 24)
        super().__init__(name)


type_map = {
    'client': BaseNode,
    'router': Router,
    'cloud': Cloud
}


def generate_script(script, *generators):
    for generator in generators:
        for cmd in generator:
            script.append(cmd)


def main(argv):
    # First, load the topology from file
    with open(argv[1]) as f:
        topology = yaml.load(f)

    # initialize each node object
    for node in topology['nodes']:
        node_init = type_map[node['type']](node['name'])
        node['init'] = node_init

    # Generate the script to run
    script = []
    gen = functools.partial(generate_script, script)

    gen(
        # create the namespaces
        *(node['init'].build_namespace() for node in topology['nodes']),

        # create the veths
        *(build_veth(
            (link['source']['name'], link['source'].get('port')),
            (link['destination']['name'], link['destination'].get('port'))
        ) for link in topology['links'])

    )

    print('#!/bin/bash')
    for l in script:
        print(sudo(l))


if __name__ == "__main__":
    import sys
    main(sys.argv)
