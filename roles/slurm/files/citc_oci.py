import asyncio
import base64
import re
import subprocess
import time
from typing import Dict, Optional, Tuple

import oci  # type: ignore
import yaml

__all__ = ["get_nodespace", "start_node"]


def load_yaml(filename: str) -> dict:
    with open(filename, "r") as f:
        return yaml.safe_load(f)


def get_nodespace() -> Dict[str, str]:
    """
    Get the information about the space into which we were creating nodes
    This will be static for all nodes in this cluster
    """
    return load_yaml("/etc/citc/startnode.yaml")


def get_subnet(oci_config, compartment_id: str, vcn_id: str) -> str:
    """
    Get the relevant cluster subnet for a given compartment, VCN and AD
    """
    return [s.id for s in oci.core.VirtualNetworkClient(oci_config).list_subnets(compartment_id, vcn_id=vcn_id).data if s.display_name == "Private"][0]


def get_node_state(oci_config, log, compartment_id: str, hostname: str) -> str:
    """
    Get the current node state of the VM for the given hostname
    If there is no such VM, return "TERMINATED"
    """
    matches = oci.core.ComputeClient(oci_config).list_instances(compartment_id=compartment_id, display_name=hostname).data
    still_exist = [i for i in matches if i.lifecycle_state != "TERMINATED"]
    if not still_exist:
        return "TERMINATED"
    if len(still_exist) > 1:
        log.error(f"{hostname}: Multiple matches found for {hostname}")
    return still_exist[0].lifecycle_state


def create_node_config(oci_config, hostname: str, ip: Optional[str], nodespace: Dict[str, str], ssh_keys: str) -> oci.core.models.LaunchInstanceDetails:
    """
    Create the configuration needed to create ``hostname`` in ``nodespace`` with ``ssh_keys``
    """
    features = subprocess.run(["sinfo", "--Format=features:200", "--noheader", f"--nodes={hostname}"], stdout=subprocess.PIPE).stdout.decode().split(',')
    ad_number = [f for f in features if f.startswith("ad=")][0].split("=")[1].strip()
    ad = f"{nodespace['ad_root']}{ad_number}"
    shape = [f for f in features if f.startswith("shape=")][0].split("=")[1].strip()
    subnet = get_subnet(oci_config, nodespace["compartment_id"], nodespace["vcn_id"])
    image_name = "Oracle-Linux-7.9-Gen2-GPU-2020.11.10-0" if "GPU" in shape else "Oracle-Linux-7.9-2020.11.10-1"
    image = get_images()[image_name][nodespace["region"]]

    with open("/home/slurm/bootstrap.sh", "rb") as f:
        user_data = base64.b64encode(f.read()).decode()

    instance_details = oci.core.models.LaunchInstanceDetails(
        compartment_id=nodespace["compartment_id"],
        availability_domain=ad,
        shape=shape,
        subnet_id=subnet,
        image_id=image,
        display_name=hostname,
        hostname_label=hostname,
        create_vnic_details=oci.core.models.CreateVnicDetails(private_ip=ip, subnet_id=subnet) if ip else None,
        metadata={
            "ssh_authorized_keys": ssh_keys,
            "user_data": user_data,
        }
    )

    return instance_details


def get_ip(hostname: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    host_dns_match = re.match(r"(\d+\.){3}\d+", subprocess.run(["host", hostname], stdout=subprocess.PIPE).stdout.decode().split()[-1])
    dns_ip = host_dns_match.group(0) if host_dns_match else None

    slurm_dns_match = re.search(r"NodeAddr=((\d+\.){3}\d+)", subprocess.run(["scontrol", "show", "node", hostname], stdout=subprocess.PIPE).stdout.decode())
    slurm_ip = slurm_dns_match.group(1) if slurm_dns_match else None

    ip = dns_ip or slurm_ip

    return ip, dns_ip, slurm_ip


async def start_node(log, host: str, nodespace: Dict[str, str], ssh_keys: str) -> None:
    log.info(f"{host}: Starting")
    oci_config = oci.config.from_file()

    while get_node_state(oci_config, log, nodespace["compartment_id"], host) == "TERMINATING":
        log.info(f"{host}:  host is currently terminating. Waiting...")
        await asyncio.sleep(5)

    node_state = get_node_state(oci_config, log, nodespace["compartment_id"], host)
    if node_state != "TERMINATED":
        log.warning(f"{host}:  host is already running with state {node_state}")
        return

    ip, _dns_ip, slurm_ip = get_ip(host)

    instance_details = create_node_config(oci_config, host, ip, nodespace, ssh_keys)

    loop = asyncio.get_event_loop()
    retry_strategy_builder = oci.retry.RetryStrategyBuilder()
    retry_strategy_builder.add_max_attempts(max_attempts=10).add_total_elapsed_time(total_elapsed_time_seconds=600)
    retry_strategy = retry_strategy_builder.get_retry_strategy()
    client = oci.core.ComputeClient(oci_config, retry_strategy=retry_strategy)

    try:
        instance_result = await loop.run_in_executor(None, client.launch_instance, instance_details)
        instance = instance_result.data
    except oci.exceptions.ServiceError as e:
        log.error(f"{host}:  problem launching instance: {e}")
        return

    if not slurm_ip:
        node_id = instance.id
        while not oci.core.ComputeClient(oci_config).list_vnic_attachments(instance_details.compartment_id, instance_id=node_id).data:
            log.info(f"{host}:  No VNIC attachment yet. Waiting...")
            await asyncio.sleep(5)

        vnic_id = oci.core.ComputeClient(oci_config).list_vnic_attachments(instance_details.compartment_id, instance_id=node_id).data[0].vnic_id
        private_ip = oci.core.VirtualNetworkClient(oci_config).get_vnic(vnic_id).data.private_ip

        log.info(f"{host}:   Private IP {private_ip}")
        subprocess.run(["scontrol", "update", f"NodeName={host}", f"NodeAddr={private_ip}"])

    log.info(f"{host}:  Started")
    return instance

def terminate_instance(log, hosts):

    config = oci.config.from_file()

    nodespace = get_nodespace()
    for host in hosts:
        log.info(f"Stopping {host}")

        try:
            matching_nodes = oci.core.ComputeClient(config).list_instances(nodespace["compartment_id"], display_name=host).data
            node_id = [n.id for n in matching_nodes if n.lifecycle_state not in {"TERMINATED", "TERMINATING"}][0]

            oci.core.ComputeClient(config).terminate_instance(node_id)
        except Exception as e:
            log.error(f" problem while stopping: {e}")
            continue

    log.info(f" Stopped {host}")


def get_images() -> Dict[str, Dict[str, str]]:
    """
    From https://docs.cloud.oracle.com/iaas/images/
    """
    return {
        "Oracle-Linux-7.9-Gen2-GPU-2020.11.10-0": {
            "ap-chuncheon-1": "ocid1.image.oc1.ap-chuncheon-1.aaaaaaaabwwu4uhkvnwfs6ast6nc3sn43rbhig6cwmnoexvilurlsuv4c4nq",
            "ap-hyderabad-1": "ocid1.image.oc1.ap-hyderabad-1.aaaaaaaahliockmravup36jjflyecwketojfmdtkhxaax65urkplif4fboka",
            "ap-melbourne-1": "ocid1.image.oc1.ap-melbourne-1.aaaaaaaa3jd7bfucshqf4yahkndxtt6psmu4e7gnpfwcou2iaywa563ot72a",
            "ap-mumbai-1": "ocid1.image.oc1.ap-mumbai-1.aaaaaaaaxan4wocugyzcht3toh6mhcakltmxsyem4ipt2zsrirgfntczxqja",
            "ap-osaka-1": "ocid1.image.oc1.ap-osaka-1.aaaaaaaa7ovj6wqi5ntzas6qmmcitod6mejg4unuyt4ngo7xm3akhxlpbesq",
            "ap-seoul-1": "ocid1.image.oc1.ap-seoul-1.aaaaaaaaxrupkrmtokkw4nwtd3sumi475wpi347q55wkyeynbyekzgtt3hja",
            "ap-sydney-1": "ocid1.image.oc1.ap-sydney-1.aaaaaaaarfxet27epkknrxcwnenol4xxxekwhjxkawsrjdrpywd3u4jtnf4a",
            "ap-tokyo-1": "ocid1.image.oc1.ap-tokyo-1.aaaaaaaaq7ypxho64hcariezzyfefgsoo6y6zh4waqv37pbpgwul6uuut7oa",
            "ca-montreal-1": "ocid1.image.oc1.ca-montreal-1.aaaaaaaahg2nhp6kh6fjj64w62qjd5uih6t6j4x2q6vg3xhouvmwmxfzjv7q",
            "ca-toronto-1": "ocid1.image.oc1.ca-toronto-1.aaaaaaaatodropartay2sqjjvbsx63dsywf4ixwvvvot7lodqbuqdjeuodja",
            "eu-amsterdam-1": "ocid1.image.oc1.eu-amsterdam-1.aaaaaaaaizqldhgelvi7wjmcbygjdw7kf7ohx4cslqmzzcxleunyftuiovta",
            "eu-frankfurt-1": "ocid1.image.oc1.eu-frankfurt-1.aaaaaaaagavzvhaa5ngdicwqm3bttqekc7vxb5qpvk6erf6enyayoiftjyba",
            "eu-zurich-1": "ocid1.image.oc1.eu-zurich-1.aaaaaaaas3ffbxgtq56o4puzq3z7lywif4f23vywyxqqgbr72rsfi2h4vasq",
            "me-dubai-1": "ocid1.image.oc1.me-dubai-1.aaaaaaaaulss5li5g7nwb4uc67op4scsikv3xevsodirexumxs2fgxp7ahba",
            "me-jeddah-1": "ocid1.image.oc1.me-jeddah-1.aaaaaaaa4ra3jts6cab2b3kdvo5cm6ya3o7qhrxaryupt5qduhmtyzxxn43q",
            "sa-santiago-1": "ocid1.image.oc1.sa-santiago-1.aaaaaaaayczdfud4bdncfbnq7agblbshdcswkxsd7qcab3qy3nbjl7ev7edq",
            "sa-saopaulo-1": "ocid1.image.oc1.sa-saopaulo-1.aaaaaaaamtlth7qztj6ds2vdg5k46itiirzxrjwwzzbn6ga7k4u6itt633dq",
            "uk-cardiff-1": "ocid1.image.oc1.uk-cardiff-1.aaaaaaaa6kaseg5sxhxlxsyt7tugdlwztaflpzrch457wlova6qjx6amtrjq",
            "uk-london-1": "ocid1.image.oc1.uk-london-1.aaaaaaaavz4whs6i3uhckwtgxjcwkyyobfrfmptxl25zw2ibikqjpidrwqva",
            "us-ashburn-1": "ocid1.image.oc1.iad.aaaaaaaag3s2smtrzrkdvrtengr2lakb6qavcvtdcfiwicmcsxcp7twiarfq",
            "us-gov-ashburn-1": "ocid1.image.oc3.us-gov-ashburn-1.aaaaaaaatq2lmuf6j2vihcnzrz6aeltd2pisbznfymcl26vsk6lq2tb4kcaq",
            "us-gov-chicago-1": "ocid1.image.oc3.us-gov-chicago-1.aaaaaaaa4amayntmaop6abh66vckfks34how7tlqgoyxjqzbfc4tw6g4237q",
            "us-gov-phoenix-1": "ocid1.image.oc3.us-gov-phoenix-1.aaaaaaaaeys5ty7jvp5yg445erv2n5t6szrwbizlnhkfkqb7hs6jqf5wtunq",
            "us-langley-1": "ocid1.image.oc2.us-langley-1.aaaaaaaaehcope5qujxe4feoix56asymjym3yxukqvgt7ogv4wv5ujqjtncq",
            "us-luke-1": "ocid1.image.oc2.us-luke-1.aaaaaaaaqxluhhoxoxpmapqhpr6fcc5vvr4vzbjoudfftbqunjqafqqt27ra",
            "us-phoenix-1": "ocid1.image.oc1.phx.aaaaaaaaz3d4hrs4jxxj5ue3fvg3pwr4zseixqhvvielziklfzjruxn55kpq",
            "us-sanjose-1": "ocid1.image.oc1.us-sanjose-1.aaaaaaaajshjnsvrpitury7qu4nsibevoyos2r5s3ztbulzqou3fdh65aifa",
        },
        "Oracle-Linux-7.9-2020.11.10-1": {
            "ap-chuncheon-1": "ocid1.image.oc1.ap-chuncheon-1.aaaaaaaaelfp7gtaodq3w6sq3s3dqwtgr7b2ofo6z5tkh6nsp6622xopmeja",
            "ap-hyderabad-1": "ocid1.image.oc1.ap-hyderabad-1.aaaaaaaav7gmok247t2jngmtyahgcktphcj5gin7bpyc2fjg3bzho47ws7ea",
            "ap-melbourne-1": "ocid1.image.oc1.ap-melbourne-1.aaaaaaaausio3ssmcxawnqwzyolpbvakwt7jsdps7o4edzxhs4gol5kd2d4a",
            "ap-mumbai-1": "ocid1.image.oc1.ap-mumbai-1.aaaaaaaazw753qijtnuynq6wrd3rmiayquc3kpijc7j5akprmvyzhcdhxsxq",
            "ap-osaka-1": "ocid1.image.oc1.ap-osaka-1.aaaaaaaahfv5tiogcrhesqedn7bfp2bm65eszn47bv6fgsepenscf2bz4bga",
            "ap-seoul-1": "ocid1.image.oc1.ap-seoul-1.aaaaaaaa5df7nz7fgtiqfbnx2fyefgsqvr5z7me4g2snwwmhgiwxgs5iozsq",
            "ap-sydney-1": "ocid1.image.oc1.ap-sydney-1.aaaaaaaa47h6zbuz3glgprnlftbzaq47b2egblcqzllshzjvotfgj7oyfnya",
            "ap-tokyo-1": "ocid1.image.oc1.ap-tokyo-1.aaaaaaaazgoy6klsxzbi5jh5kx2qwxw6l6mqtlbo4c4kak4zes7zwytd4z2q",
            "ca-montreal-1": "ocid1.image.oc1.ca-montreal-1.aaaaaaaaom7gj5nbeedakcg5ivoli2t6634o3ymyyf3sdikatskfwt4bfzja",
            "ca-toronto-1": "ocid1.image.oc1.ca-toronto-1.aaaaaaaadsv6are52igmc63fe7xkdtj22uqqzibkps6ukhupac6dwuiqby4a",
            "eu-amsterdam-1": "ocid1.image.oc1.eu-amsterdam-1.aaaaaaaahxjr3fbnv62kt5pvsblj5u7t3tfoa5bga4rv6nbapafen4ft4bua",
            "eu-frankfurt-1": "ocid1.image.oc1.eu-frankfurt-1.aaaaaaaaf6gm7xvn7rhll36kwlotl4chm25ykgsje7zt2b4w6gae4yqfdfwa",
            "eu-zurich-1": "ocid1.image.oc1.eu-zurich-1.aaaaaaaaddo5ksklg5ctvwhkncxv675ah3a5n7r7hti234ty46jt7o4i5owq",
            "me-dubai-1": "ocid1.image.oc1.me-dubai-1.aaaaaaaapjwkms5kb637ddq7ew5tjflxtgyyxted2zvzn7klnid77mjtiowa",
            "me-jeddah-1": "ocid1.image.oc1.me-jeddah-1.aaaaaaaamaasqxfymhi3ppcqn4onqjiu7wpz4gjbeem4ww3mtfq3zflruzya",
            "sa-saopaulo-1": "ocid1.image.oc1.sa-saopaulo-1.aaaaaaaa7inha53kcyutiqdbz3w4gvms2ab5z3bc624loheugh7fbvg4wada",
            "uk-cardiff-1": "ocid1.image.oc1.uk-cardiff-1.aaaaaaaakiyy4e47557phn4cymjgmaauodty7imys47vrzvdzyhci4stgm7q",
            "uk-london-1": "ocid1.image.oc1.uk-london-1.aaaaaaaai2rckqhxpvhjb6vtxdgzga3nomcqb3rl54o7wdotnof2qm2ek55a",
            "us-ashburn-1": "ocid1.image.oc1.iad.aaaaaaaaf2wxqc6ee5axabpbandk6ji27oyxyicatqw5iwkrk76kecqrrdyq",
            "us-gov-ashburn-1": "ocid1.image.oc3.us-gov-ashburn-1.aaaaaaaadqzao57flqwkih4uoocghkgwp7qelrgj5vyih4ptuuah3alkgsta",
            "us-gov-chicago-1": "ocid1.image.oc3.us-gov-chicago-1.aaaaaaaanploag6l4h653ct2r4xvqn2xwfntsjuzhmypbqpqqfuyf43qo2va",
            "us-gov-phoenix-1": "ocid1.image.oc3.us-gov-phoenix-1.aaaaaaaablheqkh4k2mo4l5wfnpg2t5zuokmgai5cex6kell4epiio5yi6lq",
            "us-langley-1": "ocid1.image.oc2.us-langley-1.aaaaaaaan444pc2rvauh4xsi47g3bffub5ow4o7uz72yxc7sb5dbobrg4yia",
            "us-luke-1": "ocid1.image.oc2.us-luke-1.aaaaaaaa7sffhf7uouur6t6amby4nuntt3r76f3z4i4jg3z6dm7m5oe4n4xq",
            "us-phoenix-1": "ocid1.image.oc1.phx.aaaaaaaaxdnx3den32vwplngpeu44zakw7lxup7vcdd3jmke4pfleaug3m6q",
            "us-sanjose-1": "ocid1.image.oc1.us-sanjose-1.aaaaaaaaunhdpihc57bc6dzipgwvhr2ouoxw65tgabx6pwgmk5qqpjtzm5oq",
        },
    }
