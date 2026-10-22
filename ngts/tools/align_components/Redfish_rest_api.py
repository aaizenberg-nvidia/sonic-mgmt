import json
import urllib.parse

import requests
import urllib3
from ip_utils import bracket_for_url

# Suppress the warning for insecure requests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class RedFishHTTPStatusCode:
    OK = 200
    Created = 201
    Accepted = 202
    No_Content = 204
    Moved_Permanently = 301
    Found = 302
    Not_Modified = 304
    Bad_Request = 400
    Unauthorized = 401
    Forbidden = 403
    Not_Found = 404
    Method_Not_Allowed = 405
    Not_Acceptable = 406
    Conflict = 409
    Gone = 410
    Length_Required = 411
    Precondition_Failed = 412
    Unsupported_Media_Type = 415
    Precondition_Required = 428
    Request_Header_Field_Too_Large = 431
    Internal_Server_Error = 500
    Not_Implemented = 501
    Service_Unavailable = 503
    Insufficient_Storage = 507


class RedFishRestApi:
    def __init__(self, ip, user, password):
        self.ip = ip
        self.username = user
        self.password = password
        print(f"[redfish] base URL https://{bracket_for_url(self.ip)} (ip={self.ip})")

    def _gen_url(self, endpoint):
        url = urllib.parse.urljoin(f"https://{bracket_for_url(self.ip)}", endpoint)
        return url

    def get_query(self, endpoint):
        """
        Perform a GET request to the specified URL with basic authentication.

        :param endpoint: The URL to send the GET request to.
        :return: The response data if the request is successful, otherwise an error message.
        """
        url = self._gen_url(endpoint)
        try:
            response = requests.get(url, auth=(self.username, self.password), verify=False)
        except Exception:
            raise

        if response.status_code <= RedFishHTTPStatusCode.Accepted:
            return response.json()
        else:
            raise Exception(f"Request failed with status code: {response.status_code}, Response text: {response.text}")

    def get_data_query(self, endpoint, file_path):
        """
        Perform a GET request to the specified URL with basic authentication.

        :param endpoint: The URL to send the GET request to.
        :param file_path: The path to the file to be downloaded.
        :return: The response data if the request is successful, otherwise an error message.
        """
        url = self._gen_url(endpoint)
        try:
            response = requests.get(url, auth=(self.username, self.password), verify=False, stream=True)
        except Exception:
            raise

        if response.status_code <= RedFishHTTPStatusCode.Accepted:
            with open(file_path, 'wb') as file:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)
            file.close()
        else:
            raise Exception(f"Request failed with status code: {response.status_code}, Response text: {response.text}")

    def post_query(self, endpoint, data=""):
        url = self._gen_url(endpoint)
        headers = {'Content-Type': 'application/json'}
        try:
            if data:
                response = requests.post(url, auth=(self.username, self.password), headers=headers,
                                         data=json.dumps(data), verify=False)
            else:
                response = requests.post(url, auth=(self.username, self.password), headers=headers, verify=False)
        except Exception:
            raise

        if response.status_code <= RedFishHTTPStatusCode.No_Content:
            return (response.json(), response.headers) if response.content else (None, None)
        else:
            raise Exception(f"Request failed with status code: {response.status_code}, Response text: {response.text}")

    def post_data_query(self, endpoint, file_path):
        """
        Perform a POST request to upload a file to the specified URL with basic authentication.

        :param endpoint: The URL to send the POST request to.
        :param file_path: The path to the file to be uploaded.
        :return: The response data if the upload is successful, otherwise an error message.
        """
        url = self._gen_url(endpoint)
        headers = {"Content-Type": "application/octet-stream"}
        with open(file_path, 'rb') as file:
            try:
                response = requests.post(url, auth=(self.username, self.password), headers=headers, data=file,
                                         verify=False)
            except Exception:
                file.close()
                raise

            if response.status_code <= RedFishHTTPStatusCode.Accepted:
                return response.json()
            else:
                raise Exception(
                    f"File upload failed with status code: {response.status_code}, Response text: {response.text}")

    def post_multipart_update(self, endpoint, file_path, targets_list=None, force_update=True):
        """
        Perform a multipart/form-data POST with Redfish update parameters
        and an update file.

        :param endpoint: Redfish endpoint (e.g., "/redfish/v1/UpdateService/update-multipart").
        :param file_path: Path to the firmware package file (e.g., .fwpkg).
        :param targets_list: Optional target string or list of target strings.
        :param force_update: Whether to force the update (default True).
        """
        url = self._gen_url(endpoint)
        update_params = {"ForceUpdate": bool(force_update)}
        if targets_list is not None:
            update_params["Targets"] = targets_list

        with open(file_path, 'rb') as file:
            files = {
                'UpdateParameters': (None, json.dumps(update_params), 'application/json'),
                'UpdateFile': (file_path, file, 'application/octet-stream')
            }
            try:
                response = requests.post(url, auth=(self.username, self.password), files=files, verify=False)
            except Exception:
                file.close()
                raise

        if response.status_code <= RedFishHTTPStatusCode.Accepted:
            return response.json() if response.content else None
        else:
            raise Exception(
                "Multipart update failed with status code: "
                f"{response.status_code}, "
                f"Response text: {response.text}"
            )

    def patch_query(self, endpoint, data, header=None):
        """
        Perform a PATCH request with a JSON payload to the specified URL with basic authentication.

        :param header: headers to append to the query
        :param endpoint: The URL to send the PATCH request to.
        :param data: The JSON payload to be sent in the PATCH request.
        :return: The response data if the request is successful, otherwise an error message.
        """
        if header is None:
            header = {}
        url = self._gen_url(endpoint)
        headers = {'Content-Type': 'application/json'}
        headers = headers.update(header)
        try:
            response = requests.patch(url, auth=(self.username, self.password), headers=headers, data=json.dumps(data),
                                      verify=False)
        except Exception:
            raise

        if response.status_code <= RedFishHTTPStatusCode.Accepted:
            return response.json() if response.content else None
        else:
            raise Exception(f"Request failed with status code: {response.status_code}, Response text: {response.text}")
