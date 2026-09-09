class InterviewDemoPlugin:
    PREFIX = "interview_demo"

    def __init__(
        self,
        plugin_config,
        namespace,
        executor,
        loco_plugin=None,
        rpc_proxy=None
    ):
        self._loco = loco_plugin
        self._rpc_proxy = rpc_proxy
    def get_tool(self) -> dict:
        return {
            "name": "interview_demo",
            "type": "actuator",
            "multiInstance": False,
            "description": "面试演示：让 GO2 说话并执行动作",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["perform"]
                    },
                    "text": {
                        "type": "string",
                        "description": "让 GO2 说的话"
                    },
                    "gesture": {
                    "type": "string",
                    "enum": ["hello", "heart", "stretch"],
                    "description": "让 GO2 做的动作"
                }
                },
                "required": ["action"],
                "x-action-params": {
                    "perform": {
                        "params": ["text", "gesture"],
                        "description": "让 GO2 说话并执行动作"
                    }
                }
            }
        }

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def dispatch(self, action: str, args: dict) -> dict | None:
        if action == "start":
            return {"state": "ready"}

        if action == "stop":
            return {"state": "idle"}

        if action == "perform":
            if self._loco is None:
                return {
                    "state": "error",
                    "message": "没有找到 GO2 动作插件"
                }
            if self._rpc_proxy is None:
                return {
                    "state": "error",
                    "message": "没有找到 GO2 音频 RPC"
                }

            text = args.get(
                "text",
                "你好，我是 GO2，很高兴认识你"
            )
            gesture = args.get(
                "gesture",
                "hello"
            )
            if gesture not in ["hello", "heart", "stretch"]:
                return {
                    "state": "error",
                    "message": "不支持的 GO2 动作"
                }
            speech_result = self._rpc_proxy.Audio_TtsMaker(
                text,
                0
            )
           
            gesture_result = self._loco.dispatch(gesture, {})
            return {
                "state": "done",
                "speech": speech_result,
                "gesture": gesture,
                "result": gesture_result
            }

        return None

    