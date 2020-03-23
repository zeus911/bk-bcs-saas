# -*- coding: utf-8 -*-
#
# Tencent is pleased to support the open source community by making 蓝鲸智云PaaS平台社区版 (BlueKing PaaS Community Edition) available.
# Copyright (C) 2017-2019 THL A29 Limited, a Tencent company. All rights reserved.
# Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
import logging
import contextlib
import traceback
from dataclasses import dataclass
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError

from backend.utils.client import make_kubectl_client, make_kubectl_client_from_kubeconfig, make_kubehelm_client
from backend.bcs_k8s.kubectl.exceptions import KubectlError, KubectlExecutionError
from backend.bcs_k8s.kubehelm.exceptions import HelmExecutionError, HelmError

logger = logging.getLogger(__name__)


@dataclass
class AppDeployer:
    """ AppDeployEngine manages app's deploy operations
    """
    app: object
    access_token: str
    kubeconfig_content: str = None
    ignore_empty_access_token: bool = False
    extra_inject_source: dict = None

    @contextlib.contextmanager
    def make_kubectl_client(self):
        with make_kubectl_client(
                project_id=self.app.project_id,
                cluster_id=self.app.cluster_id,
                access_token=self.access_token) as (client, err):
            yield client, err

    @contextlib.contextmanager
    def make_kubehelm_client(self):
        with make_kubehelm_client(
                project_id=self.app.project_id,
                cluster_id=self.app.cluster_id,
                access_token=self.access_token) as (client, err):
            yield client, err

    def install_app_by_helm(self):
        """通过helm实例化"""
        self.run_with_helm("install")

    def install_app_by_kubectl(self):
        """通过kubectl实例化"""
        self.run_with_kubectl("install")

    def upgrade_app_by_helm(self):
        """通过helm升级 app"""
        self.run_with_helm("upgrade")

    def upgrade_app_by_kubectl(self):
        """通过kubectl升级 app"""
        self.run_with_kubectl("install")

    def uninstall_app_by_helm(self):
        """通过helm删除/卸载 app"""
        self.run_with_helm("uninstall")

    def uninstall_app_by_kubectl(self):
        """通过kubectl删除/卸载 app"""
        self.run_with_kubectl("uninstall")

    def rollback_app_by_helm(self):
        """通过helm回滚app版本"""
        self.run_with_helm("rollback")

    def rollback_app_by_kubectl(self):
        """通过kubectl回滚app版本"""
        self.run_with_kubectl("install")

    def run_with_helm(self, operation):
        # NOTE: 兼容先前
        if operation == "install" or operation == "upgrade":
            content = self.app.render_app(
                access_token=self.access_token,
                username=self.app.updator,
                ignore_empty_access_token=self.ignore_empty_access_token,
                extra_inject_source=self.extra_inject_source
            )[0]
        elif operation == "uninstall":
            content = self.app.release.content
        elif operation == "rollback":
            content = self.app.release.content
        else:
            raise ValidationError("not allow operation")
        if not content:
            return
        # 保存为release的content
        self.update_app_release_content(content)
        # 使用helm执行相应的命令
        with self.make_kubehelm_client() as (client, err):
            if err is not None:
                transitioning_message = "make helm client failed, %s" % err
                self.app.set_transitioning(False, transitioning_message)
                return
            else:
                self.run_with_kubehelm_core(
                    self.app.name,
                    self.app.namespace,
                    content,
                    operation,
                    client
                )

    def get_release_revision(self, cmd_out):
        """解析执行命令的返回
        install和upgrade的返回格式类似:
        NAME: test-redis
        LAST DEPLOYED: Thu Mar 17 17:55:48 2020
        NAMESPACE: default
        STATUS: deployed
        REVISION: 1
        TEST SUITE: None
        """
        cmd_out_list = cmd_out.decode().split("\n")
        for item in cmd_out_list:
            if "REVISION:" not in item:
                continue
            return int(item.split(" ")[-1].strip())

        raise HelmError("parse helm cmd output error")

    def run_with_kubehelm_core(self, name, namespace, content, operation, client):
        transitioning_result = True
        try:
            if operation == "install":
                # 需要解析out，获取revision信息，用于rollback
                cmd_out = client.install(
                    name=name,
                    namespace=namespace,
                    tmpl_content=content,
                    chart_name=self.app.chart.name,
                    chart_version=self.app.version,
                    chart_values=self.app.release.valuefile,
                    chart_api_version="v2"
                )[0]
                self.app.release.revision = self.get_release_revision(cmd_out)
                self.app.release.save()
            elif operation == "upgrade":
                cmd_out = client.upgrade(
                    name=name,
                    namespace=namespace,
                    tmpl_content=content,
                    chart_name=self.app.chart.name,
                    chart_version=self.app.version,
                    chart_values=self.app.release.valuefile,
                    chart_api_version="v2"
                )[0]
                self.app.release.revision = self.get_release_revision(cmd_out)
                self.app.release.save()
            elif operation == "uninstall":
                client.uninstall(name, namespace)
            elif operation == "rollback":
                client.rollback(name, namespace, self.app.release.revision)
        except HelmExecutionError as e:
            transitioning_result = False
            transitioning_message = (
                "helm command execute failed.\n"
                "Error code: {error_no}\nOutput:\n{output}").format(
                error_no=e.error_no,
                output=e.output
            )
            logger.warn(transitioning_message)
        except HelmError as e:
            err_msg = str(e)
            logger.warn(err_msg)
            # TODO: 现阶段针对删除release找不到的情况，认为是正常的
            if "not found" in err_msg and operation == "uninstall":
                transitioning_result = True
                transitioning_message = "app success %s" % operation
            else:
                transitioning_result = False
                transitioning_message = err_msg
        except Exception as e:
            err_msg = str(e)
            transitioning_result = False
            logger.warning(err_msg)
            transitioning_message = self.collect_transitioning_error_message(e)
        else:
            transitioning_result = True
            transitioning_message = "app success %s" % operation

        self.app.set_transitioning(transitioning_result, transitioning_message)

    def run_with_kubectl(self, operation):
        if operation == "uninstall":
            # just load content from release, so that avoid unnecessary render exceptions
            content = self.app.release.content
        else:
            content, _ = self.app.render_app(
                access_token=self.access_token,
                username=self.app.updator,
                ignore_empty_access_token=self.ignore_empty_access_token,
                extra_inject_source=self.extra_inject_source
            )

        if content is None:
            return

        self.update_app_release_content(content)

        if self.access_token:
            with self.make_kubectl_client() as (client, err):
                if err is not None:
                    transitioning_message = "make kubectl client failed, %s" % err
                    self.app.set_transitioning(False, transitioning_message)
                    return
                else:
                    self.run_with_kubectl_core(content, operation, client)
        elif self.ignore_empty_access_token:
            if self.kubeconfig_content:
                with make_kubectl_client_from_kubeconfig(self.kubeconfig_content) as client:
                    self.run_with_kubectl_core(content, operation, client)
            else:
                raise PermissionDenied("api access must supply valid kubeconfig")
        else:
            raise ValueError(self)

    def run_with_kubectl_core(self, content, operation, client):
        transitioning_result = True
        try:
            if operation == "install":
                client.ensure_namespace(self.app.namespace)
                client.apply(
                    template=content,
                    namespace=self.app.namespace
                )
            elif operation == "uninstall":
                client.ensure_namespace(self.app.namespace)
                client.delete_one_by_one(
                    self.app.release.extract_structure(self.app.namespace),
                    self.app.namespace
                )
                # client.delete(template=content, namespace=self.app.namespace)
            else:
                raise ValueError(operation)
        except KubectlExecutionError as e:
            transitioning_result = False
            transitioning_message = (
                "kubectl command execute failed.\n"
                "Error code: {error_no}\nOutput:\n{output}").format(
                error_no=e.error_no,
                output=e.output
            )
            logger.warn(transitioning_message)
        except KubectlError as e:
            transitioning_result = False
            logger.warn(e.message)
            transitioning_message = e.message
        except Exception as e:
            transitioning_result = False
            logger.warning(e.message)
            transitioning_message = self.collect_transitioning_error_message(e)
        else:
            transitioning_result = True
            transitioning_message = "app success %s" % operation

        self.app.set_transitioning(transitioning_result, transitioning_message)

    def collect_transitioning_error_message(self, error):
        return "{error}\n{stack}".format(
            error=error,
            stack=traceback.format_exc()
        )

    def update_app_release_content(self, content):
        release = self.app.release
        release.content = content
        release.save(update_fields=["content"])
        release.refresh_structure(self.app.namespace)
