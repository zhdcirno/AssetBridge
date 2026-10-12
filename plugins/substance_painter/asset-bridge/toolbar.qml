import QtQuick 2.7
import QtQuick.Layouts 1.0
import AlgWidgets 2.0
import AlgWidgets.Style 2.0

Row {
    id: root
    spacing: 2

    signal pullClicked()
    signal pushClicked(string app)   // 回传给谁：unreal / blender / maya

    // 连接状态（文字 + 颜色），每 2 秒探测一次中转服务
    property bool bridgeConnected: false

    Timer {
        id: statusTimer
        interval: 2000
        repeat: true
        running: true
        onTriggered: {
            var xhr = new XMLHttpRequest();
            xhr.open("GET", "http://127.0.0.1:27631/api/v1/health", false);
            try { xhr.timeout = 1500; } catch (e) { }
            try {
                xhr.send("");
                root.bridgeConnected = (xhr.status >= 200 && xhr.status < 300);
            } catch (e) {
                root.bridgeConnected = false;
            }
        }
    }

    // 连接状态指示灯
    Row {
        spacing: 4
        Rectangle {
            width: 10
            height: 10
            radius: 5
            color: root.bridgeConnected ? "#2ecc71" : "#e74c3c"
            anchors.verticalCenter: parent.verticalCenter
        }
        Text {
            text: root.bridgeConnected ? "已连接" : "未连接"
            color: root.bridgeConnected ? "#2ecc71" : "#e74c3c"
            anchors.verticalCenter: parent.verticalCenter
        }
    }

    AlgButton {
        tooltip: "从中转服务拉取最新模型（会关闭当前工程后打开新模型）"
        text: "接收模型"
        onClicked: root.pullClicked()
    }

    // 回传对象可指定（旧版只能发 UE）。用三个明确按钮，省得再开设置面板。
    AlgButton {
        tooltip: "把当前工程的贴图回传给 Unreal"
        text: "回传→UE"
        onClicked: root.pushClicked("unreal")
    }

    AlgButton {
        tooltip: "把当前工程的贴图回传给 Blender"
        text: "回传→Blender"
        onClicked: root.pushClicked("blender")
    }

    AlgButton {
        tooltip: "把当前工程的贴图回传给 Maya"
        text: "回传→Maya"
        onClicked: root.pushClicked("maya")
    }
}
