import datetime
import importlib
import sys
import time

from Constants import RedfishCollection


class Payload:
    def __init__(self, **kwargs):
        self.http_header = kwargs["HttpHeaders"]
        for entry in self.http_header:
            k, v = entry.split(':')
            self.__setattr__(k.lower(), v)


class ODataAbstract:
    o_id = ""
    o_type = ""
    o_description = ""
    o_context = ""


class ODataMessageBase(ODataAbstract):
    severity = ""
    _await_to_activate = False

    def __init__(self, **kwargs):
        self.o_id = kwargs["MessageId"]
        self.message = kwargs["Message"]
        self.message_args = kwargs.get("MessageArgs", [])
        self.resolution = kwargs.get("Resolution", "")
        self.severity = kwargs.get("Severity", "OK")
        self.error = self.severity.upper() not in ["OK", "WARNING"]
        self._await_to_activate = "AwaitToActivate" in self.o_id

    def __eq__(self, other):
        return self.o_id == other.o_id and self.message == other.message

    def __hash__(self):
        return hash((self.o_id, self.message))

    def await_activate(self):
        return self._await_to_activate


class ODataMessage(ODataMessageBase):
    """
    {
      "@odata.type": "#Message.v1_1_1.Message",
      "Message": "The task with Id '0' has started.",
      "MessageArgs": [
        "0"
      ],
      "MessageId": "TaskEvent.1.0.3.TaskStarted",
      "MessageSeverity": "OK",
      "Resolution": "None."
    }
    """

    def __init__(self, **kwargs):
        self.severity = kwargs["MessageSeverity"]
        super().__init__(**kwargs)


class ODataMessageRegistry(ODataMessageBase):
    """
    {
      "@odata.type": "#MessageRegistry.v1_4_1.MessageRegistry",
      "Message": "The target device 'MGX_FW_ERoT_BMC_0' will be updated with image 'cec1736Ecfw-01.03.0208.0000'.",
      "MessageArgs": [
        "MGX_FW_ERoT_BMC_0",
        "cec1736Ecfw-01.03.0208.0000"
      ],
      "MessageId": "Update.1.0.TargetDetermined",
      "Resolution": "None.",
      "Severity": "OK"
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class Task:
    """
    {
      "@odata.id": "/redfish/v1/TaskService/Tasks/0",
      "@odata.type": "#Task.v1_4_3.Task",
      "EndTime": "2024-07-28T07:11:39+00:00",
      "HidePayload": false,
      "Id": "0",
      "Messages": [
        {
          "@odata.type": "#Message.v1_1_1.Message",
          "Message": "The task with Id '0' has started.",
          "MessageArgs": [
            "0"
          ],
          "MessageId": "TaskEvent.1.0.3.TaskStarted",
          "MessageSeverity": "OK",
          "Resolution": "None."
        },
        ...
      ],
      "Name": "Task 0",
      "Payload": {
        "HttpHeaders": [],
        "HttpOperation": "POST",
        "JsonBody": "<discarded>",
        "TargetUri": "/redfish/v1/UpdateService"
      },
      "PercentComplete": 100,
      "StartTime": "2024-07-28T07:09:33+00:00",
      "TaskMonitor": "/redfish/v1/TaskService/Tasks/0/Monitor",
      "TaskState": "Exception",
      "TaskStatus": "Critical"
    }
    """

    start_time = ""
    end_time = ""
    percent_complete = 0
    _new_messages_count = 0
    _await_action = False
    _action = ""
    _success = False
    _buffer = 0

    def __init__(self, **kwargs):
        self.messages = []
        self._errors = []
        self.o_id = kwargs["Id"]
        self.state = kwargs["TaskState"]
        self.status = kwargs["TaskStatus"]
        self.payload = None
        self.update_messages(kwargs.get("Messages", []))
        self.start_time = kwargs.get("StartTime", "")
        self.end_time = kwargs.get("EndTime", "")

    def update_messages(self, messages):
        self._new_messages_count = 0
        for msg in messages:
            msg_type = msg["@odata.type"].split('.')[-1]
            MsgCls = getattr(importlib.import_module("rf_progress"), f"OData{msg_type}")
            od_msg = MsgCls(**msg)
            if od_msg not in self.messages:
                self.messages.append(od_msg)
                self._new_messages_count += 1

    def _print_progress(self, prefix=""):
        percent = int(self.percent_complete)
        fill = '█'
        length = 50
        filled_length = int(length * percent // 100)
        bar = fill * filled_length + '-' * (length - filled_length)
        sys.stdout.write('\r' + ' ' * self._buffer + '\r')
        sys.stdout.flush()
        progress = f'{percent:.1f}% |{bar}| {prefix}'
        sys.stdout.write(progress)
        sys.stdout.flush()
        self._buffer = len(progress)

    def publisher_monitor(self, rf_api, subscriber):
        query_task_url = f"{RedfishCollection.TASK_SERVICE}/{self.o_id}"
        monitor_respond = rf_api.get_query(query_task_url)
        self.start_time = monitor_respond["StartTime"]
        subscriber(f"Task ID {self.o_id}")
        subscriber(f"Start Time - {self.start_time}")
        while self.state == "Running":
            time.sleep(5)
            monitor_respond = rf_api.get_query(query_task_url)
            self.state = monitor_respond["TaskState"]
            self.status = monitor_respond["TaskStatus"]
            self.percent_complete = monitor_respond["PercentComplete"]
            self.update_messages(monitor_respond["Messages"])
            if self._new_messages_count > 0:
                for new_msg in self.messages[-1 * self._new_messages_count:]:
                    subscriber(new_msg.message)
                    time.sleep(0.25)

        self.end_time = monitor_respond["EndTime"]
        subscriber(f"End Time - {self.end_time}")
        self._finish(monitor_respond)
        subscriber(self._errors, finish=True, success=self._success)

    def monitor(self, rf_api):
        query_task_url = f"{RedfishCollection.TASK_SERVICE}/{self.o_id}"
        monitor_respond = rf_api.get_query(query_task_url)
        self.start_time = monitor_respond["StartTime"]
        start_time = datetime.datetime.fromisoformat(self.start_time)
        print(f"Task ID {self.o_id}")
        print(f"Start Time - {start_time}")
        self._print_progress()
        while self.state == "Running":
            time.sleep(5)
            monitor_respond = rf_api.get_query(query_task_url)
            self.state = monitor_respond["TaskState"]
            self.status = monitor_respond["TaskStatus"]
            self.percent_complete = monitor_respond["PercentComplete"]
            self.update_messages(monitor_respond["Messages"])
            if self._new_messages_count > 0:
                for new_msg in self.messages[-1 * self._new_messages_count:]:
                    self._print_progress(new_msg.message)
                    time.sleep(0.25)

        print()
        self.end_time = monitor_respond["EndTime"]
        end_time = datetime.datetime.fromisoformat(self.end_time)
        print(f"End Time - {end_time}")
        print(f"Total Time - {end_time - start_time}")
        self._finish(monitor_respond)

    def _finish(self, monitor_respond):
        self._success = self.state == "Completed" and self.status == "OK"
        if self._success and monitor_respond.get("Payload", None):
            self.update_payload(**monitor_respond["Payload"])
        else:
            self._errors = [em for em in self.messages if em.error]
        need_action = [m for m in self.messages if m.await_activate()]
        if need_action:
            self._await_action = True
            self._action = "\n".join(set([m.resolution for m in need_action]))

    def update_payload(self, **kwargs):
        self.payload = Payload(**kwargs)

    def success(self):
        return self._success

    def await_action(self):
        return self._await_action

    def get_action(self):
        return self._action

    def print_error(self):
        print(f"[{self.state}] Task exist with status {self.status}")
        for m in self._errors:
            print(f"[{m.severity}] {m.message}")

    def print_messages(self):
        print(f". [Start Time] {self.start_time}")
        for m in self.messages:
            print(f"|__ [{m.severity}] {m.message}")
            if m.resolution != "None.":
                print(f"|   |__ {m.resolution}")
        print(f"|__ [End Time] {self.end_time}")
