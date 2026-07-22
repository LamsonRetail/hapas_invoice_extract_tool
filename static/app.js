/**
 * Invoice Extractor v4 — Accounting Tool JS
 */

// ===== State =====
let allRecords = [];
let filteredRecords = [];
let selectedIds = new Set();
let nextPageToken = null;
let currentTaskId = null;
let pollInterval = null;
let processingResults = [];
let uploadResults = [];
let currentSettings = {};

// Smart filter state
let larkFilters = {
    hasAttachment: false,
    unprocessed: false,
};

// Mode selection state: "bill" (Bóc tách Hóa đơn / Chứng từ) vs "vat" (Bóc tách VAT)
let extractMode = "bill";

// Range view state (null = show all)
let rangeView = { from: null, to: null };

// ===== DOM Helpers =====
const $ = (id) => document.getElementById(id);

// ===== Init =====
document.addEventListener("DOMContentLoaded", async () => {
    setupTabNav();
    setupUploadZone();
    setupEventListeners();
    await loadSettings();

    // Load saved filter state
    try {
        const saved = JSON.parse(localStorage.getItem("larkFilters") || "{}");
        if (saved.hasAttachment) {
            larkFilters.hasAttachment = true;
            $("filterHasAttachment").checked = true;
        }
        if (saved.unprocessed) {
            larkFilters.unprocessed = true;
            $("filterUnprocessed").checked = true;
        }
    } catch (e) {}
});

// ===== Tab Navigation =====
function setupTabNav() {
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const tab = btn.dataset.tab;
            switchToTab(tab);
            // Lazy-load larkbase records
            if (tab === "larkbase" && allRecords.length === 0) {
                if (isConfigured()) {
                    loadRecords();
                } else {
                    showLarkState("error");
                    $("errorMessage").innerHTML = 'Chưa có cấu hình. Vui lòng vào tab <strong>Cài đặt</strong>.';
                }
            }
        });
    });
}

function switchToTab(tabName) {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    const btn = document.querySelector(`[data-tab="${tabName}"]`);
    if (btn) btn.classList.add("active");
    document.querySelectorAll(".tab-panel").forEach(p => p.style.display = "none");
    const panel = $(`tabPanel${tabName.charAt(0).toUpperCase() + tabName.slice(1)}`);
    if (panel) panel.style.display = "block";
}

// ===== Mode Selection =====
function updateModeUI() {
    const hint = $("modeDefinitionHint");
    const thead = $("uploadResultsHead");
    if (extractMode === "bill") {
        if (hint) {
            hint.innerHTML = '<strong>💡 Bóc tách Hóa đơn / Chứng từ:</strong> Dùng cho bill chuyển khoản, ủy nhiệm chi, chứng từ giao dịch ngân hàng. Trích xuất: <em>Bên A (TK Trích nợ)</em>, <em>Số tiền trích nợ</em>, <em>Bên B (Người hưởng)</em>, <em>Nội dung thanh toán</em>, <em>Thời gian xử lý</em> & <em>Tệp đính kèm</em>.';
        }
        if (thead) {
            thead.innerHTML = `<tr>
                <th class="col-stt">STT</th>
                <th>Tệp hóa đơn</th>
                <th>Bên A (Trích nợ)</th>
                <th class="col-before">Số tiền trích nợ</th>
                <th>Bên B (Người hưởng)</th>
                <th>Nội dung thanh toán</th>
                <th>Thời gian xử lý</th>
                <th class="col-status">Trạng thái</th>
            </tr>`;
        }
    } else {
        if (hint) {
            hint.innerHTML = '<strong>💡 Bóc tách VAT:</strong> Dùng cho hoá đơn tài chính / GTGT. Trích xuất: <em>Số HĐ</em>, <em>Ngày HĐ</em>, <em>Người bán</em>, <em>Người mua</em>, <em>Có VAT?</em>, <em>Tiền trước thuế</em>, <em>Tiền VAT</em> & <em>Tổng cộng</em>.';
        }
        if (thead) {
            thead.innerHTML = `<tr>
                <th class="col-stt">STT</th>
                <th>Tên file</th>
                <th>Số HĐ</th>
                <th>Ngày HĐ</th>
                <th>Người bán</th>
                <th>Người mua</th>
                <th class="col-vat">VAT?</th>
                <th class="col-before">Trước thuế</th>
                <th class="col-vatamt">Tiền VAT</th>
                <th class="col-before">Tổng cộng</th>
                <th class="col-status">Trạng thái</th>
            </tr>`;
        }
    }
    renderUploadResults();
}

// ===== Upload Zone =====
function setupUploadZone() {
    const zone = $("dropzone");
    const input = $("fileInput");

    $("btnBrowseFiles").addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
        if (input.files.length > 0) handleFiles(Array.from(input.files));
        input.value = "";
    });

    zone.addEventListener("dragover", (e) => {
        e.preventDefault();
        zone.classList.add("drag-over");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
    zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("drag-over");
        const files = Array.from(e.dataTransfer.files);
        if (files.length > 0) handleFiles(files);
    });
}

async function handleFiles(files) {
    if (!currentSettings.openai_api_key) {
        alert("⚠️ Chưa cấu hình OpenAI API Key. Vui lòng vào tab Cài đặt.");
        switchToTab("settings");
        return;
    }

    $("uploadProgressSection").style.display = "flex";
    $("uploadResultsSection").style.display = "block";

    for (let i = 0; i < files.length; i++) {
        $("uploadProgressText").textContent = `Đang xử lý ${i + 1} / ${files.length} file...`;
        const formData = new FormData();
        formData.append("files", files[i]);

        try {
            const res = await fetch("/api/upload", { method: "POST", body: formData });
            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || "Lỗi máy chủ (HTTP " + res.status + ")");
            }
            const data = await res.json();
            uploadResults = uploadResults.concat(data.results || []);
        } catch (e) {
            uploadResults.push({
                filename: files[i].name,
                error: e.message,
                trang_thai: "LỖI"
            });
        }
        renderUploadResults();
    }

    $("uploadProgressSection").style.display = "none";

    // Auto push to Lark Base if checked
    if ($("chkAutoPushLark") && $("chkAutoPushLark").checked) {
        await pushUploadResultsToLark();
    }
}

function renderUploadResults() {
    if (uploadResults.length === 0) {
        $("uploadResultsSection").style.display = "none";
        return;
    }
    $("uploadResultsSection").style.display = "block";
    $("uploadResultsCount").textContent = `${uploadResults.length} file`;

    const tbody = $("uploadResultsBody");
    tbody.innerHTML = uploadResults.map((r, i) => {
        const isError = r.trang_thai === "LỖI" || r.error;
        let statusHtml = "";
        if (isError) {
            const errText = r.error || r.trang_thai;
            statusHtml = `<div style="display:flex; flex-direction:column; align-items:center; gap:4px;">
                <span class="badge badge-error">LỖI</span>
                <span style="font-size: 11px; color: var(--danger); max-width: 150px; text-align: center; line-height: 1.2;">${truncate(errText, 100)}</span>
            </div>`;
        }
        else if (r.trang_thai && r.trang_thai.startsWith("OK")) statusHtml = '<span class="badge badge-ok">OK</span>';
        else statusHtml = `<span class="badge badge-ok">${truncate(r.trang_thai || "OK", 12)}</span>`;

        if (extractMode === "bill") {
            const benA = r.ben_a || r.ten_nguoi_ban || "-";
            const benB = r.ben_b || r.ten_nguoi_mua || "-";
            const soTien = r.so_tien_trich_no != null ? formatMoney(r.so_tien_trich_no) : (r.tong_thanh_toan != null ? formatMoney(r.tong_thanh_toan) : "-");
            const thoiGian = r.thoi_gian_xu_ly || r.ngay_hoa_don || "-";
            const noiDung = r.noi_dung || "-";

            return `<tr${isError ? ' class="row-error"' : ''}>
                <td class="center">${i + 1}</td>
                <td title="${r.filename}">📄 ${truncate(r.filename || "-", 26)}</td>
                <td title="${benA}">${truncate(benA, 24)}</td>
                <td class="number" style="font-weight:600;">${soTien}</td>
                <td title="${benB}">${truncate(benB, 24)}</td>
                <td title="${noiDung}">${truncate(noiDung, 30)}</td>
                <td>${thoiGian}</td>
                <td class="center">${statusHtml}</td>
            </tr>`;
        } else {
            let vatBadge = r.co_vat
                ? '<span class="badge badge-vat">Có VAT</span>'
                : '<span class="badge badge-no-vat">Không</span>';
            if (isError) vatBadge = '<span class="badge badge-error">Lỗi</span>';

            return `<tr${isError ? ' class="row-error"' : ''}>
                <td class="center">${i + 1}</td>
                <td title="${r.filename}">${truncate(r.filename || "-", 26)}</td>
                <td>${r.so_hoa_don || "-"}</td>
                <td>${r.ngay_hoa_don || "-"}</td>
                <td title="${r.ten_nguoi_ban || ""}">${truncate(r.ten_nguoi_ban || "-", 22)}</td>
                <td title="${r.ten_nguoi_mua || ""}">${truncate(r.ten_nguoi_mua || "-", 22)}</td>
                <td class="center">${vatBadge}</td>
                <td class="number">${formatMoney(r.tien_truoc_thue)}</td>
                <td class="number">${formatMoney(r.tien_vat)}</td>
                <td class="number">${formatMoney(r.tong_thanh_toan)}</td>
                <td class="center">${statusHtml}</td>
            </tr>`;
        }
    }).join("");
}

async function pushUploadResultsToLark() {
    if (uploadResults.length === 0) {
        alert("Chưa có kết quả bóc tách nào để đẩy lên Lark Base!");
        return;
    }
    const larkUrl = $("uploadLarkUrl").value.trim() || currentSettings.lark_upload_url || "https://o4pvcegwn6b.sg.larksuite.com/base/Eaiabgix8a7mwqs63molRe6UgYf?table=tblYjTMstZfy1Ua6&view=vewViuZElT";
    if (!larkUrl) {
        alert("Vui lòng nhập đường dẫn Lark Base nhận kết quả!");
        return;
    }

    const btn = $("btnPushUploadLark");
    const statusMsg = $("pushStatusMsg");
    if (btn) { btn.disabled = true; btn.textContent = "Đang đẩy..."; }
    if (statusMsg) { statusMsg.textContent = "⏳ Đang đẩy lên Lark Base..."; statusMsg.className = "push-status-msg pending"; }

    try {
        const res = await fetch("/api/lark/push-upload", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                results: uploadResults,
                lark_url: larkUrl,
                mode: extractMode
            }),
        });

        let data = {};
        try {
            data = await res.json();
        } catch (err) {
            throw new Error(`Lỗi máy chủ (${res.status} ${res.statusText})`);
        }

        if (!res.ok) {
            throw new Error(data.detail || "Không thể đẩy lên Lark Base");
        }

        if (statusMsg) {
            statusMsg.textContent = `✅ ${data.message}`;
            statusMsg.className = "push-status-msg success";
        }
    } catch (e) {
        if (statusMsg) {
            statusMsg.textContent = `❌ ${e.message}`;
            statusMsg.className = "push-status-msg error";
        }
        alert("❌ Lỗi đẩy Lark Base: " + e.message);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="17 8 12 3 7 8"/>
                <line x1="12" y1="3" x2="12" y2="15"/>
            </svg> Đẩy lên Lark Base`;
        }
    }
}

// ===== Event Listeners =====
function setupEventListeners() {
    // Mode radio switches
    document.querySelectorAll('input[name="extractMode"]').forEach(radio => {
        radio.addEventListener("change", (e) => {
            extractMode = e.target.value;
            updateModeUI();
        });
    });

    // Upload tab
    $("btnClearUpload").addEventListener("click", () => {
        uploadResults = [];
        const statusMsg = $("pushStatusMsg");
        if (statusMsg) statusMsg.textContent = "";
        renderUploadResults();
    });
    $("btnExportUpload").addEventListener("click", () => exportExcelData(uploadResults));
    $("btnPushUploadLark").addEventListener("click", pushUploadResultsToLark);

    // Larkbase tab
    $('btnRefresh').addEventListener('click', refreshRecords);
    $('btnRetry').addEventListener('click', loadRecords);
    $('btnGoSettings').addEventListener('click', () => switchToTab('settings'));
    $('btnApplyRange').addEventListener('click', applyRangeSelection);
    $('btnClearRange').addEventListener('click', clearRangeSelection);
    $('btnProcess').addEventListener('click', startProcessing);
    $('btnExportExcel').addEventListener('click', () => exportExcelData(processingResults));
    $('btnDismissSummary').addEventListener('click', () => {
        $('resultsSummarySection').style.display = 'none';
    });
    $('btnLoadMore').addEventListener('click', loadMoreRecords);

    // Smart filter toggles
    $("filterHasAttachment").addEventListener("change", (e) => {
        larkFilters.hasAttachment = e.target.checked;
        saveFilterState();
        applyFilter();
    });
    $("filterUnprocessed").addEventListener("change", (e) => {
        larkFilters.unprocessed = e.target.checked;
        saveFilterState();
        applyFilter();
    });

    // Settings tab
    $("btnResolveUrl").addEventListener("click", resolveUrl);
    $("btnSetupColumns").addEventListener("click", setupColumns);
    $("btnSaveSettings").addEventListener("click", saveSettingsForm);
    $("sApiProvider").addEventListener("change", handleProviderChange);
}

function saveFilterState() {
    localStorage.setItem("larkFilters", JSON.stringify(larkFilters));
}

// ===== Settings =====
function isConfigured() {
    return !!(currentSettings.lark_app_id && currentSettings.lark_app_token && currentSettings.lark_table_id);
}

async function loadSettings() {
    try {
        const res = await fetch("/api/settings");
        currentSettings = await res.json();
        populateSettingsForm(currentSettings);
        if (isConfigured()) updateConnectionStatus(true);
    } catch (e) {
        console.error("Cannot load settings:", e);
    }
}

function populateSettingsForm(s) {
    $("sLarkAppId").value = s.lark_app_id || "";
    $("sLarkAppSecret").value = s.lark_app_secret || "";
    $("sOpenaiKey").value = s.openai_api_key || "";
    $("sOpenaiBaseUrl").value = s.openai_base_url || "https://api.openai.com/v1";
    $("sLarkUrl").value = s.lark_table_url || "";

    const uploadUrl = s.lark_upload_url || "https://o4pvcegwn6b.sg.larksuite.com/base/Eaiabgix8a7mwqs63molRe6UgYf?table=tblYjTMstZfy1Ua6&view=vewViuZElT";
    if ($("uploadLarkUrl")) $("uploadLarkUrl").value = uploadUrl;
    if ($("sLarkUploadUrl")) $("sLarkUploadUrl").value = uploadUrl;

    // Sync model select dropdown
    const savedModel = s.openai_model || "gpt-4o";
    const sel = $("sOpenaiModelSelect");
    const customInput = $("sOpenaiModel");
    let matched = false;
    for (let opt of sel.options) {
        if (opt.value === savedModel) { sel.value = savedModel; matched = true; break; }
    }
    if (!matched) {
        sel.value = "__custom__";
        customInput.style.display = "block";
        customInput.value = savedModel;
    } else {
        customInput.style.display = "none";
        customInput.value = savedModel;
    }

    // Set provider select value based on base_url
    const detected = detectProvider(s.openai_base_url, s.openai_model);
    $("sApiProvider").value = detected;
    const rf = s.lark_result_fields || {};
    $("sColVat").value = rf.co_vat || "AI_Có_VAT";
    $("sColTruocThue").value = rf.tien_truoc_thue || "AI_Tiền_Trước_Thuế";
    $("sColTienVat").value = rf.tien_vat || "AI_Tiền_VAT";
    $("sColTrangThai").value = rf.trang_thai || "AI_Trạng_Thái";
    $("sColNgayHD").value = rf.ngay_hoa_don || "AI_Ngày_HĐ";
    $("sColSoHD").value = rf.so_hoa_don || "AI_Số_HĐ";
    $("sColNguoiBan").value = rf.ten_nguoi_ban || "AI_Người_Bán";
    $("sColNguoiMua").value = rf.ten_nguoi_mua || "AI_Người_Mua";

    if (s.lark_app_token && s.lark_table_id) {
        showResolvedInfo(s.lark_app_token, s.lark_table_id, s.lark_view_id || "");
        loadFieldsForSelectors(s.lark_app_token, s.lark_table_id, s.lark_attachment_field, s.lark_content_field);
    }
}

function getResultFieldsFromForm() {
    return {
        co_vat: $("sColVat").value || "AI_Có_VAT",
        tien_truoc_thue: $("sColTruocThue").value || "AI_Tiền_Trước_Thuế",
        tien_vat: $("sColTienVat").value || "AI_Tiền_VAT",
        trang_thai: $("sColTrangThai").value || "AI_Trạng_Thái",
        ngay_hoa_don: $("sColNgayHD").value || "AI_Ngày_HĐ",
        so_hoa_don: $("sColSoHD").value || "AI_Số_HĐ",
        ten_nguoi_ban: $("sColNguoiBan").value || "AI_Người_Bán",
        ten_nguoi_mua: $("sColNguoiMua").value || "AI_Người_Mua",
    };
}

async function resolveUrl() {
    const url = $("sLarkUrl").value.trim();
    if (!url) { alert("Vui lòng nhập URL bảng Lark"); return; }
    $("btnResolveUrl").textContent = "Đang kết nối...";
    $("btnResolveUrl").disabled = true;
    try {
        const res = await fetch("/api/lark/resolve", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Không thể parse URL");
        }
        const data = await res.json();
        currentSettings.lark_app_token = data.app_token;
        currentSettings.lark_table_id = data.table_id;
        currentSettings.lark_view_id = data.view_id || "";
        currentSettings.lark_base_url = data.base_url || currentSettings.lark_base_url;
        showResolvedInfo(data.app_token, data.table_id, data.view_id);
        await loadFieldsForSelectors(data.app_token, data.table_id);
    } catch (e) {
        alert("Lỗi: " + e.message);
    } finally {
        $("btnResolveUrl").textContent = "Kết nối";
        $("btnResolveUrl").disabled = false;
    }
}

function showResolvedInfo(appToken, tableId, viewId) {
    $("resolvedInfo").style.display = "block";
    $("infoAppToken").textContent = appToken || "—";
    $("infoTableId").textContent = tableId || "—";
    $("infoViewId").textContent = viewId || "(default)";
}

async function loadFieldsForSelectors(appToken, tableId, selectedAtt = "", selectedContent = "") {
    $("fieldSelectors").style.display = "none";
    try {
        const res = await fetch(`/api/lark/fields?app_token=${encodeURIComponent(appToken)}&table_id=${encodeURIComponent(tableId)}`);
        if (!res.ok) return;
        const data = await res.json();
        const fields = data.fields || [];
        const attSel = $("sAttField");
        const contentSel = $("sContentField");
        attSel.innerHTML = '<option value="">-- Chọn cột --</option>';
        contentSel.innerHTML = '<option value="">-- Chọn cột --</option>';
        fields.forEach(f => {
            const opt1 = new Option(f.name, f.name);
            const opt2 = new Option(f.name, f.name);
            if (f.name === selectedAtt) opt1.selected = true;
            if (f.name === selectedContent) opt2.selected = true;
            attSel.appendChild(opt1);
            contentSel.appendChild(opt2);
        });
        $("fieldSelectors").style.display = "grid";
    } catch (e) {
        console.error("Cannot load fields:", e);
    }
}

async function setupColumns() {
    const appToken = currentSettings.lark_app_token || "";
    const tableId = currentSettings.lark_table_id || "";
    if (!appToken || !tableId) { alert("Vui lòng kết nối bảng Lark trước"); return; }
    const resultFields = getResultFieldsFromForm();
    $("btnSetupColumns").disabled = true;
    $("setupColumnsMsg").textContent = "Đang tạo...";
    try {
        const res = await fetch("/api/lark/setup-columns", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ app_token: appToken, table_id: tableId, result_fields: resultFields }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail);
        const msgs = [];
        if (data.created.length > 0) msgs.push(`✅ Đã tạo: ${data.created.join(", ")}`);
        if (data.already_existed.length > 0) msgs.push(`ℹ️ Đã có: ${data.already_existed.join(", ")}`);
        $("setupColumnsMsg").textContent = msgs.join(" | ");
        $("setupColumnsMsg").className = "inline-msg success";
    } catch (e) {
        $("setupColumnsMsg").textContent = "Lỗi: " + e.message;
        $("setupColumnsMsg").className = "inline-msg error";
    } finally {
        $("btnSetupColumns").disabled = false;
    }
}

async function saveSettingsForm() {
    const uploadUrl = ($("sLarkUploadUrl") ? $("sLarkUploadUrl").value.trim() : "") || ($("uploadLarkUrl") ? $("uploadLarkUrl").value.trim() : "");
    const data = {
        lark_app_id: $("sLarkAppId").value.trim(),
        lark_app_secret: $("sLarkAppSecret").value.trim(),
        openai_api_key: $("sOpenaiKey").value.trim(),
        openai_model: getSelectedModel(),
        openai_base_url: $("sOpenaiBaseUrl").value.trim(),
        lark_table_url: $("sLarkUrl").value.trim(),
        lark_upload_url: uploadUrl,
        lark_app_token: currentSettings.lark_app_token || "",
        lark_table_id: currentSettings.lark_table_id || "",
        lark_view_id: currentSettings.lark_view_id || "",
        lark_attachment_field: $("sAttField").value,
        lark_content_field: $("sContentField").value,
        lark_result_fields: getResultFieldsFromForm(),
    };
    if (currentSettings.lark_base_url) data.lark_base_url = currentSettings.lark_base_url;
    $("btnSaveSettings").disabled = true;
    $("saveSettingsMsg").textContent = "Đang lưu...";
    try {
        const res = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
        });
        if (!res.ok) throw new Error("Lỗi lưu cài đặt");
        currentSettings = { ...currentSettings, ...data };
        $("saveSettingsMsg").textContent = "✅ Đã lưu!";
        $("saveSettingsMsg").className = "inline-msg success";
        updateConnectionStatus(true);
        setTimeout(() => { $("saveSettingsMsg").textContent = ""; }, 3000);
    } catch (e) {
        $("saveSettingsMsg").textContent = "❌ " + e.message;
        $("saveSettingsMsg").className = "inline-msg error";
    } finally {
        $("btnSaveSettings").disabled = false;
    }
}

// ===== API Provider Helpers =====
function detectProvider(baseUrl, model) {
    if (!baseUrl) return "openai";
    const url = baseUrl.toLowerCase();
    if (url.includes("api.deepseek.com")) return "deepseek";
    if (url.includes("googleapis.com")) return "gemini";
    if (url.includes("openrouter.ai")) return "openrouter";
    return "custom";
}

function handleProviderChange() {
    const provider = $("sApiProvider").value;
    const baseUrlInput = $("sOpenaiBaseUrl");
    const modelInput = $("sOpenaiModel");

    if (provider === "openai") {
        baseUrlInput.value = "https://api.openai.com/v1";
        modelInput.value = "gpt-4o";
    } else if (provider === "deepseek") {
        baseUrlInput.value = "https://api.deepseek.com";
        modelInput.value = "deepseek-v4-flash";
    } else if (provider === "gemini") {
        baseUrlInput.value = "https://generativelanguage.googleapis.com/v1beta/openai";
        setModelSelect("gemini-2.5-flash");
    } else if (provider === "openrouter") {
        baseUrlInput.value = "https://openrouter.ai/api/v1";
        setModelSelect("google/gemini-2.5-flash");
    } else if (provider === "openai") {
        baseUrlInput.value = "https://api.openai.com/v1";
        setModelSelect("gpt-4o");
    } else if (provider === "deepseek") {
        baseUrlInput.value = "https://api.deepseek.com";
        setModelSelect("deepseek-v4-flash");
    } else if (provider === "custom") {
        baseUrlInput.value = "";
        setModelSelect("__custom__");
        baseUrlInput.focus();
    }
}

function getSelectedModel() {
    const sel = $("sOpenaiModelSelect");
    if (sel.value === "__custom__") {
        return $("sOpenaiModel").value.trim();
    }
    return sel.value;
}

function setModelSelect(val) {
    const sel = $("sOpenaiModelSelect");
    const customInput = $("sOpenaiModel");
    sel.value = val;
    if (val === "__custom__") {
        customInput.style.display = "block";
        customInput.focus();
    } else {
        customInput.style.display = "none";
        customInput.value = val;
    }
}

function handleModelSelectChange(sel) {
    const customInput = $("sOpenaiModel");
    if (sel.value === "__custom__") {
        customInput.style.display = "block";
        customInput.value = "";
        customInput.focus();
    } else {
        customInput.style.display = "none";
        customInput.value = sel.value;
    }
}

// ===== Records (Larkbase) =====
async function loadRecords() {
    showLarkState("loading");
    try {
        const res = await fetch("/api/lark/records?page_size=100");
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Không thể kết nối Lark");
        }
        const data = await res.json();
        allRecords = data.items || [];
        nextPageToken = data.next_page_token || null;
        updateConnectionStatus(true);
        applyFilter();
        showLarkState("records");
        $("recordCount").textContent = `${allRecords.length} dòng`;
        if (nextPageToken) $("pagination").style.display = "block";
    } catch (e) {
        updateConnectionStatus(false);
        $("errorMessage").innerHTML = e.message;
        showLarkState("error");
    }
}

async function loadMoreRecords() {
    if (!nextPageToken) return;
    try {
        const res = await fetch(`/api/lark/records?page_size=100&page_token=${nextPageToken}`);
        const data = await res.json();
        allRecords = allRecords.concat(data.items || []);
        nextPageToken = data.next_page_token || null;
        applyFilter();
        $("recordCount").textContent = `${allRecords.length} dòng`;
        if (!nextPageToken) $("pagination").style.display = "none";
    } catch (e) { console.error("Load more error:", e); }
}

/** Load more pages silently (no table re-render) until we have enough records. */
async function loadUntilRow(targetRow) {
    while (allRecords.length < targetRow && nextPageToken) {
        try {
            const res = await fetch(`/api/lark/records?page_size=100&page_token=${nextPageToken}`);
            const data = await res.json();
            allRecords = allRecords.concat(data.items || []);
            nextPageToken = data.next_page_token || null;
            // Update count display while loading
            $("recordCount").textContent = `${allRecords.length} dòng`;
            if (!nextPageToken) $("pagination").style.display = "none";
        } catch (e) {
            console.error("Auto-load error:", e);
            break;
        }
    }
    // Re-apply smart filters to include newly loaded records
    applyFilter();
}

function refreshRecords() {
    allRecords = [];
    selectedIds.clear();
    nextPageToken = null;
    loadRecords();
}

// ===== Smart Filter =====
function applyFilter() {
    filteredRecords = allRecords.filter(r => {
        if (larkFilters.hasAttachment && !r.has_attachments) return false;
        if (larkFilters.unprocessed && r.is_processed) return false;
        return true;
    });
    renderTable();
    updateSelectedCount();
}

// ===== Render Table (Larkbase) =====
function renderTable() {
    const tbody = $("recordsBody");
    if (filteredRecords.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:40px;color:var(--text-muted)">Không có dữ liệu</td></tr>`;
        return;
    }

    // Determine which slice to display based on rangeView
    let displayRecords = filteredRecords;
    let displayOffset = 0; // 0-based index of first displayed record
    if (rangeView.from !== null && rangeView.to !== null) {
        const fromIdx = Math.max(0, rangeView.from - 1);
        const toIdx = Math.min(filteredRecords.length, rangeView.to);
        displayRecords = filteredRecords.slice(fromIdx, toIdx);
        displayOffset = fromIdx;
    }

    if (displayRecords.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:40px;color:var(--text-muted)">Không có dòng nào trong khoảng này</td></tr>`;
        return;
    }

    tbody.innerHTML = displayRecords.map((rec, i) => {
        const globalIdx = displayOffset + i; // 0-based index in filteredRecords
        const stt = globalIdx + 1;           // 1-based display number
        const isSelected = selectedIds.has(rec.record_id);
        const hasAtt = rec.has_attachments;
        const rowClass = [isSelected ? "selected" : "", !hasAtt ? "no-attachment" : ""].filter(Boolean).join(" ");
        const attHtml = hasAtt
            ? rec.attachments.map(a => `<span class="attachment-tag">📎 ${truncate(a, 15)}</span>`).join("")
            : '<span class="no-file-tag">Không có file</span>';
        let vatBadge = "";
        if (rec.co_vat === "Có") vatBadge = '<span class="badge badge-vat">Có VAT</span>';
        else if (rec.co_vat === "Không") vatBadge = '<span class="badge badge-no-vat">Không</span>';
        else if (rec.is_processed) vatBadge = `<span class="badge badge-skip">${rec.co_vat || '-'}</span>`;
        else vatBadge = '<span class="badge badge-pending">—</span>';
        let statusHtml = "";
        if (rec.trang_thai) {
            const st = String(rec.trang_thai);
            if (st.startsWith("OK")) statusHtml = '<span class="badge badge-ok">OK</span>';
            else if (st.startsWith("CẢNH BÁO")) statusHtml = '<span class="badge badge-warn">Cảnh báo</span>';
            else if (st.startsWith("LỖI")) statusHtml = '<span class="badge badge-error">Lỗi</span>';
            else if (st.startsWith("Bỏ qua")) statusHtml = '<span class="badge badge-skip">Bỏ qua</span>';
            else statusHtml = `<span class="badge badge-ok">${truncate(st, 15)}</span>`;
        } else {
            statusHtml = '<span class="badge badge-pending">Chưa xử lý</span>';
        }
        return `<tr class="${rowClass}" data-id="${rec.record_id}">
            <td class="center">
                <label class="checkbox-wrapper">
                    <input type="checkbox" ${isSelected ? "checked" : ""} ${!hasAtt ? "disabled" : ""}
                        onchange="toggleSelect('${rec.record_id}', this.checked)">
                    <span class="checkmark"></span>
                </label>
            </td>
            <td class="center">${stt}</td>
            <td title="${rec.noi_dung}">${truncate(rec.noi_dung || "-", 60)}</td>
            <td>${attHtml}</td>
            <td class="center">${vatBadge}</td>
            <td class="number">${formatMoney(rec.tien_truoc_thue)}</td>
            <td class="number">${formatMoney(rec.tien_vat)}</td>
            <td class="center">${statusHtml}</td>
        </tr>`;
    }).join("");
}

// ===== Selection =====
function toggleSelect(recordId, checked) {
    if (checked) selectedIds.add(recordId);
    else selectedIds.delete(recordId);
    updateSelectedCount();
    const row = document.querySelector(`tr[data-id="${recordId}"]`);
    if (row) row.classList.toggle("selected", checked);
}

// ===== Range Selection =====
async function applyRangeSelection() {
    const from = parseInt($('rangeFrom').value) || 1;
    const to = parseInt($('rangeTo').value) || filteredRecords.length;
    const clampedFrom = Math.max(1, from);
    const targetTo = Math.max(clampedFrom, to);

    // Auto-load more records if needed to cover the requested range
    if (targetTo > allRecords.length && nextPageToken) {
        const btn = $('btnApplyRange');
        const origText = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Đang tải...';
        try {
            await loadUntilRow(targetTo);
        } finally {
            btn.disabled = false;
            btn.textContent = origText;
        }
    }

    const clampedTo = Math.min(filteredRecords.length, targetTo);

    // Update range view so table shows only these rows
    rangeView.from = clampedFrom;
    rangeView.to = clampedTo;

    // Select all records with attachments in this range
    selectedIds.clear();
    for (let i = clampedFrom - 1; i < clampedTo; i++) {
        const rec = filteredRecords[i];
        if (rec && rec.has_attachments) {
            selectedIds.add(rec.record_id);
        }
    }
    updateSelectedCount();
    renderTable();
}

function clearRangeSelection() {
    // Reset range view to show all records
    rangeView.from = null;
    rangeView.to = null;
    $('rangeFrom').value = '1';
    $('rangeTo').value = '';
    selectedIds.clear();
    updateSelectedCount();
    renderTable();
}

function updateSelectedCount() {
    $('selectedCount').textContent = `${selectedIds.size} đã chọn`;
    $('btnProcess').disabled = selectedIds.size === 0;
    // Always update rangeTo placeholder with current total
    if ($('rangeTo')) {
        $('rangeTo').placeholder = `Đến (${filteredRecords.length})`;
    }
}

// ===== Processing (Larkbase) =====
async function startProcessing() {
    if (selectedIds.size === 0) return;
    const recordIds = Array.from(selectedIds);
    $("processingOverlay").style.display = "flex";
    try {
        const res = await fetch("/api/lark/process", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ record_ids: recordIds }),
        });
        if (!res.ok) {
            const err = await res.json();
            alert("Lỗi: " + (err.detail || "Không thể xử lý"));
            $("processingOverlay").style.display = "none";
            return;
        }
        const data = await res.json();
        currentTaskId = data.task_id;
        pollInterval = setInterval(pollProcessingStatus, 2000);
    } catch (e) {
        alert("Lỗi kết nối: " + e.message);
        $("processingOverlay").style.display = "none";
    }
}

async function pollProcessingStatus() {
    if (!currentTaskId) return;
    try {
        const res = await fetch(`/api/lark/status/${currentTaskId}`);
        if (!res.ok) {
            if (res.status === 404) {
                clearInterval(pollInterval); pollInterval = null;
                $("processingOverlay").style.display = "none";
                alert("Tiến trình đã bị ngắt hoặc không tồn tại. Vui lòng làm mới trang và thử lại.");
            }
            return;
        }
        
        const data = await res.json();
        const pct = data.total > 0 ? (data.processed / data.total) * 100 : 0;
        $("progressBar").style.width = pct + "%";
        $("progressText").textContent = `${data.processed} / ${data.total} dòng`;
        if (data.current_record) $("processingRecord").textContent = `Đang xử lý: ${data.current_record}`;
        if (data.status === "done") {
            clearInterval(pollInterval); pollInterval = null;
            processingResults = data.results;
            $("processingOverlay").style.display = "none";
            showProcessingSummary(data.results);
            selectedIds.clear();
            await loadRecords();
        }
    } catch (e) { /* retry */ }
}

function showProcessingSummary(results) {
    $("statTotal").textContent = results.length;
    $("statOk").textContent = results.filter(r => r.co_vat === true).length;
    $("statNoVat").textContent = results.filter(r => r.co_vat === false && r.trang_thai !== "LỖI").length;
    $("statError").textContent = results.filter(r => r.trang_thai === "LỖI").length;
    $("resultsSummarySection").style.display = "block";
}

// ===== Export =====
async function exportExcelData(results) {
    if (!results || results.length === 0) { alert("Chưa có kết quả để xuất"); return; }
    try {
        const res = await fetch("/api/export", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ results }),
        });
        if (!res.ok) { alert("Lỗi khi xuất Excel"); return; }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = "ket_qua_boc_tach_hoa_don.xlsx";
        document.body.appendChild(a); a.click();
        document.body.removeChild(a); URL.revokeObjectURL(url);
    } catch (e) { alert("Lỗi: " + e.message); }
}

// ===== Helpers =====
function showLarkState(state) {
    $("loadingSection").style.display = state === "loading" ? "flex" : "none";
    $("errorSection").style.display = state === "error" ? "flex" : "none";
    $("recordsSection").style.display = state === "records" ? "block" : "none";
}

function updateConnectionStatus(connected) {
    const el = $("connectionStatus");
    if (connected) {
        el.className = "connection-status connected";
        el.querySelector(".status-text").textContent = "Đã kết nối Lark";
    } else {
        el.className = "connection-status error";
        el.querySelector(".status-text").textContent = "Mất kết nối";
    }
}

function formatMoney(value) {
    if (value == null || value === "" || value === 0) return "-";
    const num = typeof value === "string" ? parseFloat(value) : value;
    if (isNaN(num)) return "-";
    return new Intl.NumberFormat("vi-VN").format(Math.round(num)) + " ₫";
}

function truncate(str, maxLen) {
    if (!str) return "";
    return str.length > maxLen ? str.substring(0, maxLen) + "…" : str;
}
