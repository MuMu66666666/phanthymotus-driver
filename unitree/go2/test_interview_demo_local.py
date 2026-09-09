from interview_demo import InterviewDemoPlugin


class FakeLoco:
    def dispatch(self, action, args):
        print("① 收到 GO2 动作命令")
        print("机器狗动作：", action)
        return {"ret": 0}


class FakeRpcProxy:
    def Audio_TtsMaker(self, text, speaker_id):
        print("② 收到 GO2 说话命令")
        print("说话内容：", text)
        print("speaker_id：", speaker_id)
        return 0

class FakeBundle:
    def __init__(self, plugins):
        self._plugins = plugins

    def dispatch(self, tool_name, args):
        for p in self._plugins:
            tool = p.get_tool()

            if tool["name"] == tool_name:
                action = args.pop("action", tool_name)
                args["_tool_name"] = tool_name
                return p.dispatch(action, args)

        return None
    

loco = FakeLoco()
rpc_proxy = FakeRpcProxy()

demo = InterviewDemoPlugin(
    {},
    "",
    None,
    loco_plugin=loco,
    rpc_proxy=rpc_proxy
)
bundle = FakeBundle([demo])
tool = demo.get_tool()

print("卡片名：", tool["name"])
print("卡片参数：", list(tool["inputSchema"]["properties"].keys()))
print("③ 点击 perform")

result = bundle.dispatch(
    "interview_demo",
    {
        "action": "perform"
    }
)
print("④ 执行完成")
print(result)