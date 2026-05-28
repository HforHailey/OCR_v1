## 📁 Core Code Architecture & Functions

The code repository is separated into four functional pillars:

### 1. Main Pipeline Control (Main)
Orchestrates application startup, system configuration parsing, and file routing routines.

| Function | Description |
| :--- | :--- |
| `main()` | Application entry point. Loads settings, scans the inbox directory for pending PDFs, and routes file batches to their designated processing handler. |
| `load_config()` | Reads and decodes `config.json`, performing an overlay merge with the hardcoded `DEFAULT_CONFIG` fallback dictionary. |
| `_deep_merge()` | A utility script that recursively handles merging nested dictionary nodes without wiping configuration subsets. |
| `housekeep_log()` | Maintenance daemon that scans `log.txt` and truncates execution trace blocks that exceed the historical retention limit. |

### 2. Barcode Recognition Engine (Barcode)
Handles matrix manipulations, image slicing, and decoding layer workflows.

| Function | Description |
| :--- | :--- |
| `read_barcode_from_image()` | Core barcode entry pipeline. Crops the target sector ➔ attempts decoding ➔ runs full-page/upscaled fallback routines if failed ➔ filters and returns the optimal values. |
| `_crop_barcode_region()` | Isolates specific document segments using relative percentage bounds specified via `barcode.region` to filter out surrounding text noise. |
| `_try_read_barcode()` | Core routing method. Feeds target graphics into the `zxing` pipeline, falling back to `pyzbar` if no string is returned. |
| `_read_barcode_zxing()` | Leverages `zxing-cpp` bindings to extract barcode information from linear arrays (Primary Engine). |
| `_read_barcode_pyzbar()` | Leverages `pyzbar` to extract barcode symbols. Automatically applies an adversarial *Otsu Threshold binarization* filter to repair low-contrast matrices. |
| `_pick_barcode()` | Resolution strategy for multi-barcode pages; calculates bounding box heights to prioritize the topmost barcode string. |

### 3. Processing Modes
Determines how documents are structurally separated, batched, or joined together.

| Function | Description |
| :--- | :--- |
| `process_mode1()` | **Strict Mode (Page-by-Page Merge):** Enforces that every single PDF page must contain a valid barcode. Pages returning matching barcode keys are merged into a unified document; pages lacking a valid barcode are routed directly to the Failure pool. |
| `process_mode2()` | **Grouping Mode (Leader-Follower Batching):** A page with a barcode initiates a new group. Subsequent pages lacking a barcode are continuously appended to this group until a new barcode is discovered, triggering a new file sequence. |

### 4. PDF & File Utilities
Interacts with the local operating system storage layers, file input/output streams, and PDF syntax libraries.

| Function | Description |
| :--- | :--- |
| `pdf_to_first_page_image()` | Converts the target page of a PDF document into a PIL Image instance via `pdf2image` (Poppler) for graphic processing. |
| `merge_pdfs()` | Merges multiple independent PDF streams into a singular continuous file stream using `pypdf`. |
| `validate_length()` | Evaluates extracted barcode values against minimum and maximum length restrictions declared in your system properties. |
| `build_filename()` | Formats final filenames by parsing token expressions (e.g., `{value}_{timestamp}`) declared in templates. |
| `safe_path()` | Filename collision protection module. Appends millisecond timestamps if a filename already exists in the target directory to prevent overwrites. |
| `ensure_dirs()` | Directory bootstrapper. Validates and generates critical execution nodes (`Output`, `Failed`, `Archive`) on launch. |
| `_get_output_dir()` | Dictates the final file location, resolving relative links and dynamically constructing subdirectories if date-based organization is turned on. |
| `save_output()` | Orchestrates final file delivery: copies source elements to Archive ➔ constructs/moves processed arrays ➔ commits output directly to the targeted directory. |
| `archive_files()` | Non-destructive backup utility. Clones incoming raw scans straight into an immutable `Archive/run_ts/` path before altering data structures. |
| `move_to_failed()` | Isolation handler. Backups affected items into the Archive segment and flushes raw components over to the `Failed` tree for operator review. |
| `_get_poppler_path()` | Scans the deployment layout for portable Poppler dependencies and safely updates runtime environment vectors if discovered. |
| `_archive_enabled()` | System validation test checking if the target archival folder is accessible and grants valid write permissions. |

---
---

## 📁 系統架構與 Function 說明

系統原始碼主要由以下四大核心模組組成：

### 1. 主流程控制 (Main Flow)
負責程式的初始化、配置載入、日誌清理以及處理任務的分派。
* `main()`：程式進入點。負責讀取設定檔、掃描 `Inbox` 中的 PDF 檔案，並根據設定的模式分派給對應的處理核心。
* `load_config()`：讀取並解析 `config.json`，並與系統預設值 (`DEFAULT_CONFIG`) 進行遞迴合併。
* `_deep_merge()`：內部工具。用於遞迴合併兩個字典 (Dictionary) 物件。
* `housekeep_log()`：自動化維護。檢查 `log.txt`，自動刪除超過 N 天以上的舊執行紀錄區塊。

### 2. 條碼識別模組 (Barcode Recognition)
負責處理圖像、範圍裁切以及多引擎條碼讀取。
* `read_barcode_from_image()`：條碼核心入口。執行流程為：按設定區域裁剪 ➔ 讀取條碼 ➔ (失敗時) 觸發放大/全頁盲測 ➔ 篩選並返回最佳結果。
* `_crop_barcode_region()`：根據設定檔中定義的 `barcode.region` 比例，裁切出局部圖像以減少雜訊。
* `_try_read_barcode()`：核心讀取器。優先調用 `zxing`，若失敗則自動切換至 `pyzbar` 備用引擎。
* `_read_barcode_zxing()`：使用 `zxing-cpp` 讀取條碼（系統主識別引擎）。
* `_read_barcode_pyzbar()`：使用 `pyzbar` 讀取條碼，內部會自動加入大津二值化 (`Otsu threshold`) 以應對模糊或低對比度的條碼。
* `_pick_barcode()`：當單頁識別出多個條碼時，根據幾何位置自動挑選位於最上方 (Top-most) 的條碼。

### 3. 處理模式 (Processing Modes)
根據業務需求，決定檔案的分類與合併邏輯。
* `process_mode1()`：**每頁獨立/相同合併模式**。要求處理的每一頁 PDF 都必須包含條碼；條碼值相同的頁面會合併為同一個檔案，完全無條碼的頁面則判定為 Failed。
* `process_mode2()`：**批次分組模式**。以帶有條碼的頁面作為分組開頭 (Group Leader)，後續沒有條碼的頁面會自動追加到該組別，直到遇到下一個帶有新條碼的頁面才會開啟新分組。

### 4. PDF 與檔案工具組 (PDF & File Utilities)
負責底層的檔案系統操作、PDF 轉換、合併與安全路徑檢查。
* `pdf_to_first_page_image()`：調用 `pdf2image` (Poppler) 將 PDF 的指定頁面轉換為 PIL Image 格式供 OCR/條碼引擎辨識。
* `merge_pdfs()`：調用 `pypdf` 將多個獨立的 PDF 頁面或檔案合併輸出為單一文件。
* `validate_length()`：驗證識別出的條碼字串長度是否符合 `config.json` 定義的有效範圍。
* `build_filename()`：依據設定檔中的命名模板（例如 `{value}_{timestamp}`）動態生成最終的輸出檔名。
* `safe_path()`：路徑防撞機制。若目標路徑已存在同名檔案，會自動附加時間戳記，避免覆蓋既有檔案。
* `ensure_dirs()`：初始化環境。確保系統運作所需的 `Output`、`Failed`、`Archive` 等資料夾結構存在。
* `_get_output_dir()`：取得有效的輸出路徑，並支援在內部按當前日期自動建立子資料夾（如 `Output/2026-05-28/`）。
* `save_output()`：執行最終歸檔流程：備份原檔到 Archive ➔ 合併/移動檔案 ➔ 寫入至 Output 目錄。
* `archive_files()`：備份機制。將處理前的原始檔案複製到 `Archive/{run_timestamp}/` 子資料夾中，只複製，不刪除原檔，確保數據安全。
* `move_to_failed()`：異常處理。當檔案處理失敗時，先進行 Archive 備份，再將原檔移至 `Failed` 資料夾。
* `_get_poppler_path()`：自動定位可攜式或系統內建的 Poppler 執行檔路徑。
* `_archive_enabled()`：檢查設定檔中的歸檔路徑是否正確啟用且具備寫入權限。

---
