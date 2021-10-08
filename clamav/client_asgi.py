# Copyright (c) 2019-2020 SAP SE or an SAP affiliate company. All rights reserved. This file is
# licensed under the Apache Software License, v. 2 except as noted otherwise in the LICENSE file
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import dataclasses
import enum
import json
import logging
import typing

import requests
import urllib3
import urllib3.util.retry

import ci.util
import clamav.util


logger = logging.getLogger(__name__)


class ScanStatus(enum.Enum):
    SCAN_SUCCEEDED = 'scan_succeeded'
    SCAN_FAILED = 'scan_failed'


class MalwareStatus(enum.Enum):
    UNKNOWN = 'unknown'
    FOUND_MALWARE = 'FOUND_MALWARE'
    OK = 'OK'


@dataclasses.dataclass
class Meta:
    scanned_octets: int
    receive_duration_seconds: float
    scan_duration_seconds: float


@dataclasses.dataclass
class ScanResult:
    status: ScanStatus
    details: str
    malware_status: MalwareStatus
    meta: typing.Optional[Meta]
    name: str


class ClamAVRoutesAsgi:
    def __init__(
        self,
        base_url: str,
    ):
        self._base_url = base_url

    def scan(self):
        return ci.util.urljoin(self._base_url, 'scan')


class ClamAVClientAsgi:
    def __init__(
        self,
        routes: ClamAVRoutesAsgi,
        retry_cfg: urllib3.util.retry.Retry=None,
    ):
        self.routes = routes
        self.http = urllib3.PoolManager(retries=retry_cfg)

    def _request(self, *args, **kwargs):
        res = self.http.request(
            *args,
            **kwargs,
        )
        if res.status < 200 or res.status > 200:
            raise urllib3.exceptions.HTTPError(f'{res.status=} {res.data=}')

        body = b''
        for chunk in res.stream():
            body += chunk

        parsed = json.loads(body)
        return parsed

    def scan(
        self,
        data,
        timeout_seconds:float=60*15,
        content_length_octets:int=None,
        name: str=None,
    ) -> ScanResult:
        url = self.routes.scan()

        if content_length_octets:
            headers = {'Content-Length': str(content_length_octets)}
        else:
            headers = {}

        if name:
            headers['Name'] = clamav.util.make_latin1_encodable(name)

        try:
            response = self._request(
                method='POST',
                url=url,
                body=data,
                headers=headers,
                timeout=timeout_seconds,
                preload_content=False,
            )
        except (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            urllib3.exceptions.HTTPError,
        ) as ce:
            if (rq := getattr(ce, 'request', None)):
                rq_url = getattr(rq, 'url', '<unknown>')
                if rq_url != url:
                    url = f'{url=}, {rq_url=}'

            logger.warning(f'{name=}: {ce=} {url=}')
            return ScanResult(
                status=ScanStatus.SCAN_FAILED,
                details=f'{ce=}',
                malware_status=MalwareStatus.UNKNOWN,
                meta=None,
                name=name,
            )

        return ScanResult(
            status=ScanStatus.SCAN_SUCCEEDED,
            details=response.get('message', 'no details available'),
            malware_status=MalwareStatus(response['result']),
            meta=Meta(**response.get('meta')),
            name=name,
        )