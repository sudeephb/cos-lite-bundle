# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Metrics and logs from a machine charm are ingested over juju-info/cos_agent by COS Lite."""

import asyncio
import logging
import os
import subprocess
from types import SimpleNamespace

import pytest
from helpers import get_or_add_model
from juju.controller import Controller
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

agent = SimpleNamespace(charm="grafana-agent", name="agent")
# TODO: increase scale to 2 when CI runners are more performant
principal_cos_agent = SimpleNamespace(charm="zookeeper", name="principal-cos-agent", scale=1)
principal_juju_info = SimpleNamespace(charm="ubuntu", name="principal-juju-info", scale=1)


@pytest.mark.abort_on_fail
async def test_setup_models(ops_test: OpsTest):
    global lxd_mdl, k8s_mdl, k8s_ctl

    lxd_ctl_name = os.environ["LXD_CONTROLLER"]
    k8s_ctl_name = os.environ["K8S_CONTROLLER"]

    # The current model name is generated by pytest-operator from the test name + random suffix.
    # Use the same model name in both controllers.
    k8s_mdl_name = lxd_mdl_name = ops_test.model_name

    # We do not want to make assumptions here about the current controller.
    # Assuming a k8s controller is ready and its name is stored in $LXD_CONTROLLER.
    lxd_ctl = Controller()
    await lxd_ctl.connect(lxd_ctl_name)
    lxd_mdl = await get_or_add_model(ops_test, lxd_ctl, lxd_mdl_name)
    await lxd_mdl.set_config({"logging-config": "<root>=WARNING; unit=DEBUG"})

    # Assuming a k8s controller is ready and its name is stored in $K8S_CONTROLLER.
    k8s_ctl = Controller()
    await k8s_ctl.connect(k8s_ctl_name)
    k8s_mdl = await get_or_add_model(ops_test, k8s_ctl, k8s_mdl_name)
    await k8s_mdl.set_config({"logging-config": "<root>=WARNING; unit=DEBUG"})


@pytest.mark.abort_on_fail
async def test_deploy_cos(rendered_bundle):
    # Use CLI to deploy bundle until https://github.com/juju/python-libjuju/issues/816 is fixed.
    # await k8s_mdl.deploy(str(rendered_bundle), trust=True)
    cmd = [
        "juju",
        "deploy",
        "--trust",
        "-m",
        f"{k8s_ctl.controller_name}:{k8s_mdl.name}",
        rendered_bundle,
        "--overlay",
        "./overlays/offers-overlay.yaml",
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        logger.error(e.output.decode())
        raise


@pytest.mark.abort_on_fail
async def test_deploy_machine_charms():
    await asyncio.gather(
        # Principal
        lxd_mdl.deploy(
            principal_cos_agent.charm,
            application_name=principal_cos_agent.name,
            num_units=principal_cos_agent.scale,
            series="jammy",
            channel="edge",
        ),
        # Principal 2
        lxd_mdl.deploy(
            principal_juju_info.charm,
            application_name=principal_juju_info.name,
            num_units=principal_juju_info.scale,
            series="jammy",
        ),
        # Subordinate
        lxd_mdl.deploy(
            agent.charm,
            application_name=agent.name,
            num_units=0,
            series="jammy",
            channel="edge",
        ),
    )
    # Must relate the subordinate before any "wait for idle", because otherwise agent would be in
    # 'unknown' status.
    await lxd_mdl.add_relation(f"{principal_cos_agent.name}:cos-agent", agent.name)
    await lxd_mdl.add_relation(f"{principal_juju_info.name}:juju-info", agent.name)
    await lxd_mdl.block_until(lambda: len(lxd_mdl.applications[agent.name].units) > 0)


@pytest.mark.abort_on_fail
async def test_integration():
    # The consumed endpoint names must match offers-overlay.yaml.
    await asyncio.gather(
        lxd_mdl.consume(
            f"admin/{k8s_mdl.name}.prometheus-receive-remote-write",
            application_alias="prometheus",
            controller_name=k8s_ctl.controller_name,  # same as os.environ["K8S_CONTROLLER"]
        ),
        lxd_mdl.consume(
            f"admin/{k8s_mdl.name}.loki-logging",
            application_alias="loki",
            controller_name=k8s_ctl.controller_name,  # same as os.environ["K8S_CONTROLLER"]
        ),
        lxd_mdl.consume(
            f"admin/{k8s_mdl.name}.grafana-dashboards",
            application_alias="grafana",
            controller_name=k8s_ctl.controller_name,  # same as os.environ["K8S_CONTROLLER"]
        ),
    )

    await asyncio.gather(
        lxd_mdl.add_relation(agent.name, "prometheus"),
        lxd_mdl.add_relation(agent.name, "loki"),
        lxd_mdl.add_relation(agent.name, "grafana"),
    )

    # `idle_period` needs to be greater than the scrape interval to make sure metrics ingested.
    await asyncio.gather(
        # First, we wait for the critical phase to pass with raise_on_error=False.
        # (In CI, using github runners, we often see unreproducible hook failures.)
        lxd_mdl.wait_for_idle(timeout=1800, idle_period=180, raise_on_error=False),
        k8s_mdl.wait_for_idle(timeout=1800, idle_period=180, raise_on_error=False),
    )

    await asyncio.gather(
        # Then we wait for "active", without raise_on_error=False, so the test fails sooner in case
        # there is a persistent error status.
        lxd_mdl.wait_for_idle(status="active", timeout=7200, idle_period=180),
        k8s_mdl.wait_for_idle(status="active", timeout=7200, idle_period=180),
    )


async def test_metrics(ops_test):
    """Make sure machine charm metrics reach Prometheus."""
    # Get the values of all `juju_unit` labels in prometheus
    # Output looks like this:
    # {"status":"success","data":[
    #  "agent/0","agent/1","agent/10","agent/11","agent/2","agent/3","agent/8","agent/9",
    #  "alertmanager/0","grafana/0",
    #  "principal-cos-agent/2","principal-cos-agent/3",
    #  "principal-juju-info/0","principal-juju-info/1",
    #  "prometheus-k8s","traefik/0"
    # ]}
    cmd = [
        "juju",
        "ssh",
        "-m",
        f"{k8s_ctl.controller_name}:{k8s_mdl.name}",
        "prometheus/0",
        "curl",
        "localhost:9090/api/v1/label/juju_unit/values",
    ]
    try:
        result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        logger.error(e.stdout.decode())
        raise
    output = result.stdout.decode().strip()
    logger.info("Label values: %s", output)
    assert output.count(principal_cos_agent.name) == principal_cos_agent.scale
    assert output.count(principal_juju_info.name) == principal_juju_info.scale
    assert output.count(agent.name) >= principal_cos_agent.scale + principal_juju_info.scale


async def test_logs(ops_test):
    """Make sure machine charm logs reach Loki."""
    # Get the values of all `juju_unit` labels in loki
    # Loki uses strip_prefix, so we do need to use the ingress path
    # Output looks like this:
    # {"status":"success","data":[
    #  "principal-cos-agent/2","principal-cos-agent/3",
    #  "principal-juju-info/0","principal-juju-info/1"
    # ]}
    cmd = [
        "juju",
        "ssh",
        "-m",
        f"{k8s_ctl.controller_name}:{k8s_mdl.name}",
        "loki/0",
        "curl",
        "localhost:3100/loki/api/v1/label/juju_unit/values",
    ]
    try:
        result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        logger.error(e.stdout.decode())
        raise

    output = result.stdout.decode().strip()
    logger.info("Label values: %s", output)
    assert output.count(principal_cos_agent.name) == principal_cos_agent.scale
    assert output.count(principal_juju_info.name) == principal_juju_info.scale


async def test_dashboards(ops_test):
    # Get grafana admin password
    action = await k8s_mdl.applications["grafana"].units[0].run_action("get-admin-password")
    action = await action.wait()
    password = action.results["admin-password"]

    # Get all dashboards
    # Grafana uses strip_prefix, so we do need to use the ingress path
    # Output looks like this:
    # [
    #  {"id":6,"uid":"exunkijMk","title":"Grafana Agent Node Exporter Quickstart",
    #   "uri":"db/grafana-agent-node-exporter-quickstart",
    #   "url":"/cos-grafana/d/exunkijMk/grafana-agent-node-exporter-quickstart","slug":"",
    #   "type":"dash-db","tags":[],"isStarred":false,"sortMeta":0},
    #  {"id":4,"uid":"rYdddlPWk","title":"System Resources","uri":"db/system-resources",
    #   "url":"/cos-grafana/d/rYdddlPWk/system-resources","slug":"","type":"dash-db",
    #   "tags":["linux"],"isStarred":false,"sortMeta":0},
    #  {"id":5,"uid":"SDE76m7Zzz","title":"ZooKeeper by Prometheus",
    #   "uri":"db/zookeeper-by-prometheus",
    #   "url":"/cos-grafana/d/SDE76m7Zzz/zookeeper-by-prometheus","slug":"","type":"dash-db",
    #   "tags":["v4"],"isStarred":false,"sortMeta":0}
    # ]
    cmd = [
        "juju",
        "ssh",
        "-m",
        f"{k8s_ctl.controller_name}:{k8s_mdl.name}",
        "grafana/0",
        "curl",
        "--user",
        f"admin:{password}",
        "localhost:3000/api/search",
    ]
    try:
        result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        logger.error(e.stdout.decode())
        raise

    output = result.stdout.decode().strip()
    assert "zookeeper" in output
    assert "grafana-agent-node-exporter" in output


async def test_destroy(ops_test):
    if ops_test.keep_model:
        return

    # First, must remove the machine charms and saas, otherwise:
    # ERROR cannot destroy application "grafana": application is used by 3 consumers
    # Do not `block_until_done=True` because of the juju bug where teardown never completes.
    await asyncio.gather(
        lxd_mdl.remove_application(agent.name),
        lxd_mdl.remove_application(principal_cos_agent.name),
        lxd_mdl.remove_application(principal_juju_info.name),
    )
    await asyncio.gather(
        lxd_mdl.remove_saas("prometheus"),
        lxd_mdl.remove_saas("loki"),
        lxd_mdl.remove_saas("grafana"),
    )
    # Give it some time to settle, since we cannot block until complete.
    await asyncio.sleep(60)

    # Now remove traefik politely to avoid IP binding issues in the next test
    await k8s_mdl.remove_application("traefik")
    # Give it some time to settle, since we cannot block until complete.
    await asyncio.sleep(60)

    # The rest can be forcefully removed by pytest-operator.
