"""Native settings window for the Decision Infra gateway and exact model route."""
from __future__ import annotations

import threading

import AppKit as A
import objc
from Foundation import NSObject, NSMakeRect

import settings_config as config
import ui_style
import userconfig


PALETTE = ui_style.PALETTE


class SettingsController(NSObject):
    @objc.python_method
    def build(self):
        self.path = userconfig.env_files()[0]
        self.original = config.read_document(self.path)
        self.file_values = {key: value for key, value in
                            userconfig.parse_env_file(self.path).items()
                            if key in config.ALLOWED}
        self.initial = {}
        self.fields = {}
        self.busy = False

        self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 480, 328),
            A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable,
            A.NSBackingStoreBuffered, False)
        self.window.setAppearance_(A.NSAppearance.appearanceNamed_(A.NSAppearanceNameAqua))
        self.window.setTitle_("Jev QQ Analyst · 设置")
        self.window.setBackgroundColor_(PALETTE["bg"])
        self.window.setHasShadow_(True)
        self.window.setLevel_(A.NSFloatingWindowLevel + 1)
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        view = A.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 480, 328))
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        self.window.setContentView_(view)

        heading = self.label(view, "连接设置", 24, 272, 432, 30, 22, PALETTE["text"])
        heading.setFont_(A.NSFont.boldSystemFontOfSize_(22))
        self.label(view, "Decision Infra · 精确模型路由", 24, 250, 432, 18,
                   11, PALETTE["muted"])
        divider = ui_style.make_surface(0, PALETTE["edge"])
        divider.setFrame_(NSMakeRect(24, 232, 432, 1))
        view.addSubview_(divider)

        for name, title, label_y, field_y, default in (
                ("BASE_URL", "网关地址", 206, 170, config.DEFAULTS["DECISION_INFRA"][0]),
                ("MODEL", "模型路由", 140, 104, config.DEFAULTS["DECISION_INFRA"][1])):
            self.label(view, title, 24, label_y, 432, 20, 12, PALETTE["text"])
            field = A.NSTextField.alloc().initWithFrame_(NSMakeRect(24, field_y, 432, 30))
            key = f"DECISION_INFRA_{name}"
            value = self.file_values.get(key, default)
            field.setStringValue_(value)
            self.style_field(field)
            field.setDelegate_(self)
            field.setAccessibilityLabel_(title)
            view.addSubview_(field)
            self.fields[name] = field
            self.initial[key] = value

        self.status = self.label(view, "待测试", 24, 74, 432, 20, 11, PALETTE["muted"])
        self.test_button = self.button(view, "测试连接", "testConnection:", 24, 24, 112)
        self.label(view, "保存后重启生效", 152, 32, 185, 17, 10, PALETTE["muted"])
        self.save_button = self.button(view, "保存", "saveSettings:", 364, 24, 92, True)
        self.window.center()
        return self

    @objc.python_method
    def label(self, view, text, x, y, width, height, size=13, color=None):
        label = ui_style.make_label(text, x, y, width, height, size, color)
        label.cell().setWraps_(True)
        view.addSubview_(label)
        return label

    @objc.python_method
    def style_field(self, field):
        field.setFont_(A.NSFont.systemFontOfSize_(12))
        field.setTextColor_(PALETTE["text"])
        field.setBackgroundColor_(PALETTE["field"])
        field.setWantsLayer_(True)
        field.layer().setBorderColor_(PALETTE["edge"].CGColor())
        field.layer().setBorderWidth_(0.75)
        field.layer().setCornerRadius_(ui_style.RADIUS_FIELD)

    @objc.python_method
    def button(self, view, title, action, x, y, width, primary=False):
        button = A.NSButton.alloc().initWithFrame_(NSMakeRect(x, y, width, 32))
        button.setTitle_(title)
        ui_style.style_button(button, font_size=11, radius=9, primary=primary)
        button.setTarget_(self)
        button.setAction_(action)
        view.addSubview_(button)
        return button

    @objc.python_method
    def set_status(self, message, kind="info"):
        colors = {"info": PALETTE["muted"], "success": PALETTE["green"],
                  "error": PALETTE["red"]}
        self.status.setStringValue_(message)
        self.status.setTextColor_(colors[kind])
        self.status.setFont_(A.NSFont.boldSystemFontOfSize_(11))

    @objc.python_method
    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        A.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.python_method
    def values(self):
        return {name: str(field.stringValue()) for name, field in self.fields.items()}

    @objc.python_method
    def changed(self):
        return {f"DECISION_INFRA_{name}": value
                for name, value in self.values().items()
                if value != self.initial[f"DECISION_INFRA_{name}"]}

    def controlTextDidChange_(self, notification):
        self.set_status("已修改 · 请测试")

    def saveSettings_(self, sender):
        self.window.makeFirstResponder_(None)
        changes = self.changed()
        if not changes:
            self.set_status("没有改动")
            return
        values = self.values()
        try:
            config.validate_endpoint(values["BASE_URL"])
            if not values["MODEL"].strip():
                raise ValueError("请填写精确模型路由。")
            for name, value in values.items():
                key = f"DECISION_INFRA_{name}"
                if key not in self.file_values:
                    changes[key] = value
            self.original = config.write_settings(self.path, self.original, changes)
        except ValueError as error:
            self.set_status(str(error), "error")
            return
        except OSError:
            self.set_status("保存失败：请检查文件权限及可用磁盘空间。", "error")
            return
        self.initial.update(changes)
        self.file_values.update(changes)
        self.set_status("已保存 · 重启后生效", "success")

    def testConnection_(self, sender):
        if self.busy:
            return
        self.window.makeFirstResponder_(None)
        values = self.values()
        try:
            config.validate_endpoint(values["BASE_URL"])
            if not values["MODEL"].strip():
                raise ValueError("请填写精确模型路由。")
        except ValueError as error:
            self.set_status(str(error), "error")
            return
        self.busy = True
        self.test_button.setEnabled_(False)
        self.save_button.setEnabled_(False)
        self.set_status("正在测试连接…")

        def work():
            try:
                config.test_connection(values["BASE_URL"], values["MODEL"])
                result = None
            except Exception as error:
                result = config.error_message(error)
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "connectionFinished:", result, False)

        threading.Thread(target=work, daemon=True).start()

    def connectionFinished_(self, error):
        self.busy = False
        self.test_button.setEnabled_(True)
        self.save_button.setEnabled_(True)
        self.set_status(error or "连接成功", "error" if error else "success")

    def windowShouldClose_(self, sender):
        if self.busy:
            self.set_status("请求进行中，请等待结果后关闭。")
            return False
        if self.changed():
            alert = A.NSAlert.alloc().init()
            alert.setMessageText_("放弃尚未保存的配置？")
            alert.addButtonWithTitle_("继续编辑")
            alert.addButtonWithTitle_("放弃修改")
            return alert.runModal() == A.NSAlertSecondButtonReturn
        return True


if __name__ == "__main__":
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
    userconfig.load()
    controller = SettingsController.alloc().init().build()
    controller.show()
    app.run()
