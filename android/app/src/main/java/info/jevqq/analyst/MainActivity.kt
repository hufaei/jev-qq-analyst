package info.jevqq.analyst

import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.text.method.PasswordTransformationMethod
import android.util.TypedValue
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONObject
import java.io.File

/** Configuration and service guidance. Conversation analysis appears only while QQ is foreground. */
class MainActivity : Activity() {
    private lateinit var store: SettingsStore
    private lateinit var urlInput: EditText
    private lateinit var modelInput: EditText
    private lateinit var keyInput: EditText
    private lateinit var keySection: LinearLayout
    private lateinit var result: TextView
    private lateinit var testButton: Button
    private lateinit var serviceStatus: TextView
    private lateinit var captureStatus: TextView
    private var savedKey = ""
    private var activeMode = EndpointMode.JEV
    private val draftUrls = mutableMapOf<EndpointMode, String>()
    private val uiFont = runCatching {
        Typeface.createFromFile(File("/system/fonts/NotoSansCJK-Regular.ttc"))
    }.getOrElse { Typeface.create("sans-serif", Typeface.NORMAL) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = SettingsStore(this)
        var loadError: String? = null
        val current = try { store.load() } catch (error: Exception) {
            loadError = error.message ?: getString(R.string.settings_error)
            ConnectionSettings(EndpointMode.JEV, SettingsStore.JEV_URL)
        }
        savedKey = current.apiKey
        activeMode = current.mode
        draftUrls[EndpointMode.JEV] = store.urlFor(EndpointMode.JEV)
        draftUrls[EndpointMode.GATEWAY] = store.urlFor(EndpointMode.GATEWAY)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(24))
        }
        val scroll = ScrollView(this).apply { addView(root) }
        setContentView(scroll)

        root.addView(text(R.string.app_title, 24f))
        root.addView(text(R.string.app_summary, 14f))
        root.addView(text(R.string.endpoint_mode, 18f))
        val modes = RadioGroup(this).apply { orientation = RadioGroup.VERTICAL }
        val jev = RadioButton(this).apply { id = View.generateViewId(); setText(R.string.mode_jev) }
        val gateway = RadioButton(this).apply { id = View.generateViewId(); setText(R.string.mode_gateway) }
        modes.addView(jev)
        modes.addView(gateway)
        root.addView(modes)

        root.addView(text(R.string.endpoint_url, 16f))
        urlInput = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine(true)
            setText(current.url)
        }
        root.addView(urlInput, fullWidth())
        root.addView(text(R.string.endpoint_hint, 13f))

        root.addView(text(R.string.model_route, 16f))
        modelInput = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT
            setSingleLine(true)
            setText(current.model)
        }
        root.addView(modelInput, fullWidth())

        keySection = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        keySection.addView(text(R.string.api_key_label, 16f))
        keyInput = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setSingleLine(true)
            transformationMethod = PasswordTransformationMethod.getInstance()
            isSaveEnabled = false
            importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO
            setHint(if (savedKey.isEmpty()) R.string.api_key_new else R.string.api_key_saved)
        }
        keySection.addView(keyInput, fullWidth())
        keySection.addView(text(R.string.api_key_hint, 13f))
        keySection.addView(Button(this).apply {
            setText(R.string.clear_key)
            setOnClickListener {
                savedKey = ""
                keyInput.text.clear()
                keyInput.setHint(R.string.api_key_new)
                result.setText(R.string.key_clear_pending)
            }
        }, fullWidth())
        root.addView(keySection)

        root.addView(Button(this).apply {
            setText(R.string.save_settings)
            setOnClickListener { saveSettings() }
        }, fullWidth())
        testButton = Button(this).apply {
            setText(R.string.test_connection)
            setOnClickListener { testConnection() }
        }
        root.addView(testButton, fullWidth())
        result = text(R.string.settings_ready, 14f)
        if (loadError != null) result.text = loadError
        root.addView(result)

        root.addView(text(R.string.accessibility_title, 18f))
        root.addView(text(R.string.accessibility_guidance, 14f))
        serviceStatus = text(R.string.accessibility_off, 14f)
        root.addView(serviceStatus)
        captureStatus = text(R.string.capture_status_empty, 14f)
        root.addView(captureStatus)
        root.addView(Button(this).apply {
            setText(R.string.open_accessibility_settings)
            setOnClickListener { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }
        }, fullWidth())

        modes.check(if (activeMode == EndpointMode.JEV) jev.id else gateway.id)
        keySection.visibility = if (activeMode == EndpointMode.JEV) View.VISIBLE else View.GONE
        modes.setOnCheckedChangeListener { _, checked ->
            draftUrls[activeMode] = urlInput.text.toString()
            activeMode = if (checked == jev.id) EndpointMode.JEV else EndpointMode.GATEWAY
            urlInput.setText(draftUrls.getValue(activeMode))
            keySection.visibility = if (activeMode == EndpointMode.JEV) View.VISIBLE else View.GONE
        }
        applyTypography(root)
    }

    override fun onResume() {
        super.onResume()
        if (::serviceStatus.isInitialized) {
            val component = ComponentName(packageName, "$packageName.QqAccessibilityService")
            val enabled = Settings.Secure.getString(contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES).orEmpty()
                .split(':').any { ComponentName.unflattenFromString(it) == component }
            serviceStatus.setText(if (enabled) R.string.accessibility_on else R.string.accessibility_off)
            captureStatus.text = getSharedPreferences("capture_diagnostic", MODE_PRIVATE)
                .getString("last_qq_status", null)?.let { getString(R.string.capture_status, it) }
                ?: getString(R.string.capture_status_empty)
        }
    }

    private fun selectedSettings(): ConnectionSettings {
        val key = keyInput.text.toString().trim().ifEmpty { savedKey }
        return ConnectionSettings(activeMode, urlInput.text.toString().trim(),
            modelInput.text.toString().trim(), key).also { it.endpoint() }
    }

    private fun saveSettings() {
        try {
            val settings = selectedSettings()
            store.save(settings)
            savedKey = settings.apiKey
            keyInput.text.clear()
            keyInput.setHint(if (savedKey.isEmpty()) R.string.api_key_new else R.string.api_key_saved)
            draftUrls[activeMode] = settings.url
            result.setText(R.string.settings_saved)
        } catch (error: Exception) {
            result.text = error.message ?: getString(R.string.settings_error)
        }
    }

    private fun testConnection() {
        val settings = try { selectedSettings() } catch (error: Exception) {
            result.text = error.message ?: getString(R.string.settings_error)
            return
        }
        if (settings.mode == EndpointMode.JEV && settings.apiKey.isEmpty()) {
            result.setText(R.string.api_key_required)
            return
        }
        testButton.isEnabled = false
        result.setText(R.string.testing_connection)
        Thread {
            val outcome = runCatching {
                val spec = assets.open("decision_questions.json").bufferedReader(Charsets.UTF_8)
                    .use { JSONObject(it.readText()) }
                DecisionClient(settings, spec).judge("连接测试：请判断这是一条普通问候。")
            }
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                testButton.isEnabled = true
                result.text = outcome.fold(
                    onSuccess = { getString(R.string.connection_ok, it.backend) },
                    onFailure = { it.message ?: getString(R.string.settings_error) },
                )
            }
        }.start()
    }

    private fun text(resId: Int, size: Float) = TextView(this).apply {
        setText(resId)
        textSize = size
        setPadding(0, dp(8), 0, dp(8))
    }

    private fun applyTypography(view: View) {
        if (view is TextView) {
            view.typeface = Typeface.create(uiFont, view.typeface?.style ?: Typeface.NORMAL)
            view.setTextSize(TypedValue.COMPLEX_UNIT_PX, view.textSize * .88f)
            view.includeFontPadding = false
        }
        if (view is ViewGroup) {
            for (index in 0 until view.childCount) applyTypography(view.getChildAt(index))
        }
    }

    private fun fullWidth() = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()
}
