"""Native model settings, opened from the HUD menu. Saving requires a restart."""
from __future__ import annotations

import threading
from pathlib import Path

import AppKit as A
import objc
from Foundation import NSObject, NSMakeRect

import judge
import userconfig
import settings_config as config
import ui_style


PALETTE = ui_style.PALETTE
ACTIVE_PREFIXES = ("DECISION_INFRA",)


class SettingsController(NSObject):
    @objc.python_method
    def build(self):
        self.path = userconfig.env_files()[0]
        self.original = config.read_document(self.path)
        values = userconfig.parse_env_file(self.path)
        self.file_values = values
        self.initial = {}
        self.fields = {}
        self.controls = []
        self.busy = False
        self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 480, 328),
            A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable,
            A.NSBackingStoreBuffered, False)
        self.window.setAppearance_(A.NSAppearance.appearanceNamed_(A.NSAppearanceNameAqua))
        self.window.setTitle_("Jev · 设置")
        self.window.setBackgroundColor_(PALETTE["bg"])
        self.window.setHasShadow_(True)
        self.window.setLevel_(A.NSFloatingWindowLevel + 1)
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        view = A.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 480, 328))
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        self.window.setContentView_(view)
        title = self.label(view, "连接设置", 24, 272, 432, 30, 22, PALETTE["text"])
        title.setFont_(A.NSFont.boldSystemFontOfSize_(22))
        self.label(view, "Decision Infra · Jev", 24, 250, 432, 18, 11, PALETTE["muted"])
        divider = ui_style.make_surface(0, PALETTE["edge"])
        divider.setFrame_(NSMakeRect(24, 232, 432, 1))
        view.addSubview_(divider)

        prefix = "DECISION_INFRA"
        api_key = A.NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        api_key.setStringValue_(values.get(f"{prefix}_API_KEY", ""))
        api_key.setEnabled_(False)
        fields = {"API_KEY": api_key}
        self.initial[f"{prefix}_API_KEY"] = api_key.stringValue()
        for name, title, label_y, field_y in (
                ("BASE_URL", "网关地址", 206, 170),
                ("MODEL", "模型路由", 140, 104)):
            self.label(view, title, 24, label_y, 432, 20, 12, PALETTE["text"])
            cls = A.NSComboBox if name == "MODEL" else A.NSTextField
            field = cls.alloc().initWithFrame_(NSMakeRect(24, field_y, 432, 30))
            value = values.get(f"{prefix}_{name}", config.DEFAULTS[prefix][name == "MODEL"])
            field.setStringValue_(value)
            self.style_field(field)
            field.setDelegate_(self)
            field.setAccessibilityLabel_(title)
            if name == "MODEL":
                self.set_models(field, [])
                field.setCompletes_(False)
            view.addSubview_(field)
            fields[name] = field
            self.initial[f"{prefix}_{name}"] = value
            self.controls.append(field)
        self.fields[prefix] = fields
        self.status = self.label(view, "待测试", 24, 74, 432, 20, 11, PALETTE["muted"])
        self.test_button = self.button(view, "测试连接", "testConnection:", 24, 24, 112)
        self.test_button.setTag_(0)
        self.controls.append(self.test_button)
        self.label(view, "保存后重启生效", 152, 32, 185, 17, 10, PALETTE["muted"])
        self.save_button = self.button(view, "保存", "saveSettings:", 364, 24, 92, True)
        self.controls.append(self.save_button)
        self.window.center()
        return self

    @objc.python_method
    def set_status(self, text, kind="info"):
        colors = {"info": PALETTE["muted"],
                  "success": PALETTE["green"],
                  "error": PALETTE["red"]}
        self.status.setStringValue_(text)
        self.status.setTextColor_(colors[kind])
        self.status.setFont_(A.NSFont.boldSystemFontOfSize_(11))

    @objc.python_method
    def set_models(self, combo, models):
        current = combo.stringValue()
        combo.removeAllItems()
        combo.addItemsWithObjectValues_(models or ["暂无"])
        combo.setStringValue_(current)

    def comboBoxWillPopUp_(self, notification):
        self.model_before_popup = notification.object().stringValue()

    def comboBoxSelectionDidChange_(self, notification):
        combo = notification.object()
        if list(combo.objectValues()) == ["暂无"]:
            combo.deselectItemAtIndex_(0)
            combo.setStringValue_(getattr(self, "model_before_popup", ""))
        else:
            self.set_status("路由已修改 · 请测试")

    @objc.python_method
    def current_source(self, prefix):
        if prefix != "DECISION_INFRA":
            raise ValueError("本应用只配置 Decision Infra")
        source = userconfig.source_of("DECISION_INFRA_BASE_URL", "DECISION_INFRA_MODEL")
        summary = "本次启动：通过 Decision Infra 精确路由 Jev"
        if source == "none":
            source = "内置默认（127.0.0.1:8080 / jev-latest）"
        detail = "来源：" + source.replace(str(Path.home()), "~") + "\n以下编辑内容保存后，需重启应用才会生效。"
        return summary, detail

    @objc.python_method
    def label(self, view, text, x, y, w, h, size=13, color=None):
        field = ui_style.make_label(text, x, y, w, h, size, color)
        field.cell().setWraps_(True)
        view.addSubview_(field)
        return field

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
    def refresh_offline_section(self):
        text = "模型由 Decision Infra 管理 · 本应用不下载权重、不持有 Provider Key"
        self.offline_label.setStringValue_(text)
        self.offline_delete_btn.setHidden_(True)
        self.offline_enable_btn.setHidden_(True)

    def deleteOfflineModel_(self, sender):
        alert = A.NSAlert.alloc().init()
        alert.setMessageText_("删除离线判断模型？")
        alert.setInformativeText_("之后使用离线判断需重新下载（约 7 GB）。正在运行的应用不受影响，重启后生效。")
        alert.addButtonWithTitle_("删除")
        alert.addButtonWithTitle_("取消")
        if alert.runModal() != A.NSAlertFirstButtonReturn:
            return
        sender.setEnabled_(False)
        self.set_status("正在删除离线判断模型…")
        threading.Thread(target=self._delete_model_work, daemon=True).start()

    @objc.python_method
    def _delete_model_work(self):
        import shutil
        error = ""
        try:
            shutil.rmtree(judge.model_cache_dir())
        except OSError as e:
            error = str(e)
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "modelDeleted:", error, False)

    def modelDeleted_(self, error):
        self.refresh_offline_section()
        if error:
            self.set_status(f"删除失败：{error[:80]}", "error")
        else:
            self.set_status("已删除离线判断模型。正在运行的判断不受影响；删除的文件不可恢复。", "success")

    def enableOfflineModel_(self, sender):
        alert = A.NSAlert.alloc().init()
        alert.setMessageText_("启用离线判断？")
        alert.setInformativeText_("下次启动的预热将下载判断模型（约 7 GB，一次性），之后判断完全离线进行。")
        alert.addButtonWithTitle_("启用")
        alert.addButtonWithTitle_("取消")
        if alert.runModal() != A.NSAlertFirstButtonReturn:
            return
        try:
            self.original = config.write_settings(self.path, self.original,
                                                  {"JUDGE_BACKEND": "local"})
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        except OSError:
            self.set_status("保存失败：请检查文件权限及可用磁盘空间。", "error")
            return
        self.file_values["JUDGE_BACKEND"] = "local"
        self.refresh_offline_section()
        self.set_status("已启用离线判断（写入 JUDGE_BACKEND=local）。请退出并重新打开应用，预热时开始下载。",
                        "success")

    @objc.python_method
    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        A.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.python_method
    def values(self, prefix):
        return {k: str(v.stringValue()) for k, v in self.fields[prefix].items()}

    @objc.python_method
    def changed(self):
        return {f"{p}_{k}": v for p in ACTIVE_PREFIXES for k, v in self.values(p).items()
                if v != self.initial[f"{p}_{k}"]}

    def controlTextDidChange_(self, notification):
        field = notification.object()
        for fields in self.fields.values():
            if field in (fields["API_KEY"], fields["BASE_URL"]):
                combo = fields["MODEL"]
                self.set_models(combo, [])
        self.set_status("已修改 · 请测试")

    def saveSettings_(self, sender):
        self.window.makeFirstResponder_(None)
        changes = self.changed()
        if not changes:
            self.set_status("没有改动")
            return
        # Persist missing displayed defaults for edited services, but keep untouched key lines.
        for prefix in ACTIVE_PREFIXES:
            if any(k.startswith(prefix + "_") for k in changes):
                changes.update({f"{prefix}_{k}": v for k, v in self.values(prefix).items()
                                if k != "API_KEY" and f"{prefix}_{k}" not in self.file_values})
        try:
            for prefix in ACTIVE_PREFIXES:
                if any(k.startswith(prefix + "_") for k in changes):
                    vals = self.values(prefix)
                    if not vals["BASE_URL"].strip() or not vals["MODEL"].strip():
                        raise ValueError("请填写 Decision Infra 网关地址和精确路由模型。")
            for key, value in changes.items():
                if key.endswith("_BASE_URL") and value:
                    config.validate_endpoint(value)
            self.original = config.write_settings(self.path, self.original, changes)
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        except OSError:
            self.set_status("保存失败：请检查文件权限及可用磁盘空间。", "error")
            return
        self.initial.update(changes)
        self.file_values.update(changes)
        self.set_status("已保存 · 重启后生效", "success")

    def fetchModels_(self, sender):
        self.start_request(sender.tag(), True)

    def testConnection_(self, sender):
        self.start_request(sender.tag(), False)

    @objc.python_method
    def start_request(self, index, listing):
        if self.busy:
            return
        self.window.makeFirstResponder_(None)
        prefix = ACTIVE_PREFIXES[index]
        values = self.values(prefix)
        try:
            config.validate_endpoint(values["BASE_URL"])
            if prefix != "DECISION_INFRA" and not values["API_KEY"]:
                raise ValueError("请填写密钥；Ollama 可填写 ollama。")
            if not listing and not values["MODEL"].strip():
                raise ValueError("请填写模型后再测试。")
            extra = None
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        if listing:
            combo = self.fields[prefix]["MODEL"]
            self.set_models(combo, [])
        self.busy = True
        for control in self.controls:
            control.setEnabled_(False)
        self.set_status("正在获取模型列表…" if listing else "正在测试连接…")

        def work():
            result = {"index": index, "listing": listing}
            try:
                args = (prefix, values["BASE_URL"], values["API_KEY"])
                if listing:
                    result["models"] = config.list_models(*args)
                else:
                    config.test_connection(*args, values["MODEL"], extra)
            except Exception as e:
                result["error"] = config.error_message(e)
            self.performSelectorOnMainThread_withObject_waitUntilDone_("requestFinished:", result, False)
        threading.Thread(target=work, daemon=True).start()

    def requestFinished_(self, result):
        self.busy = False
        for control in self.controls:
            control.setEnabled_(True)
        self.fields["DECISION_INFRA"]["API_KEY"].setEnabled_(False)
        if result.get("error"):
            self.set_status(result["error"] + (" 模型仍可手填。" if result["listing"] else ""), "error")
        elif result["listing"]:
            combo = self.fields[ACTIVE_PREFIXES[result["index"]]]["MODEL"]
            self.set_models(combo, result["models"])
            self.set_status(f"已获取 {len(result['models'])} 个模型。请从下拉列表选择或手填，再测试连接。", "success")
        else:
            self.set_status("连接成功", "success")

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
