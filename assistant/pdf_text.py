"""Bounded PDF text projection in a short-lived, memory-limited worker."""
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENCODING = 'pdf-text-v1:pypdf-6.21.4'
MAX_INPUT = 2 * 1024 * 1024
MAX_PAGES = 80
MAX_CHARS = 200000
MAX_STREAM = 8 * 1024 * 1024
MAX_TOTAL_STREAM = 16 * 1024 * 1024
MAX_RESULT = 1024 * 1024
TIMEOUT = 15
MEMORY_BYTES = 512 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
LIMITATIONS = ('PDF文字層的抽取副本，含依原檔順序產生的頁次標記；引用行號指抽取文字行，不是版面座標。'
               '不是OCR或PDF畫面辨識；圖片、圖表、註解、附件與表單值不納入。'
               '複雜欄位與表格閱讀順序可能不同，可能包含隱藏或既有OCR文字，不保證等同畫面。'
               '空文字頁明確標記，不代表該頁空白；全文無文字、加密或含互動表單者不匯入。'
               '最多80頁及20萬字，不截斷後冒充全文；不執行腳本、動作或開啟外部連結。')


def metadata(document):
    if document.get('encoding', '').startswith('pdf-text-v1:'):
        return {'representation': document['encoding'], 'citation_basis': 'extracted_text_lines',
                'fingerprint_basis': 'archive_and_projected_text', 'extraction_limits': LIMITATIONS}
    return {}


def limit_worker_memory():
    """Apply before importing the parser; the returned Windows handle stays live."""
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        return None
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong), ('flags', w.DWORD),
                    ('min_working', c.c_size_t), ('max_working', c.c_size_t), ('active', w.DWORD),
                    ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class IO(c.Structure):
        _fields_ = [(name, c.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(c.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                    ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]; kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]; kernel.AssignProcessToJobObject.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]; kernel.CloseHandle.restype = w.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    if not handle: raise OSError(c.get_last_error(), 'PDF記憶體限制無法建立。')
    limits = Extended(); limits.basic.flags = 0x100; limits.process_memory = MEMORY_BYTES
    if (not kernel.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits))
            or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
        error = c.get_last_error(); kernel.CloseHandle(handle)
        raise OSError(error, 'PDF記憶體限制無法套用，未解析。')
    return handle


def extract(raw):
    """Worker-only parser entrypoint. The caller supplies process resource limits."""
    import pypdf
    if pypdf.__version__ != '6.21.4':
        raise ValueError('PDF解析器版本不符，請依requirements.txt安裝依賴。')
    if not raw.startswith(b'%PDF-') or len(raw) > MAX_INPUT:
        raise ValueError('PDF格式或大小不符。')
    from pypdf import PdfReader, apply_configuration
    with apply_configuration(
        maximum_declared_stream_length=MAX_STREAM, array_based_stream_maximum_output_length=MAX_STREAM,
        array_maximum_element_count=10000, dictionary_maximum_element_count=5000,
        zlib_maximum_output_length=MAX_STREAM, zlib_maximum_recovery_input_length=MAX_INPUT,
        lzw_maximum_output_length=MAX_STREAM, run_length_maximum_output_length=MAX_STREAM,
        brotli_maximum_output_length=MAX_STREAM, jbig2_maximum_output_length=MAX_STREAM,
        image_maximum_buffer_size=MAX_STREAM, stream_filters_maximum_length=4,
        stream_decoding_work_maximum_length=MAX_TOTAL_STREAM, stream_parsing_work_maximum_count=200000,
        page_tree_maximum_entries=500, page_tree_maximum_depth=32, page_tree_maximum_page_entries=10000,
        xform_maximum_invocations_per_extraction=100, jbig2dec_binary=None):
        reader = PdfReader(io.BytesIO(raw), strict=True, root_object_recovery_limit=0)
        if reader.is_encrypted: raise ValueError('加密PDF尚不支援；請提供未加密的文字副本。')
        form = reader.root_object.get('/AcroForm')
        if form and form.get_object().get('/Fields'):
            raise ValueError('PDF含互動表單，無法保證欄位文字完整；請先另存一般文字文件。')
        count = len(reader.pages)
        if not 1 <= count <= MAX_PAGES: raise ValueError('PDF需為1到80頁，未匯入部分內容。')
        output = []; used = 0; stream_bytes = 0; has_text = False
        for number, page in enumerate(reader.pages, 1):
            for annotation in page.get('/Annots', []):
                if annotation.get_object().get('/Subtype') == '/Widget':
                    raise ValueError('PDF含互動表單或孤立欄位，請先另存一般文字文件。')
            content = page.get_contents()
            if content is not None:
                size = len(content.get_data()); stream_bytes += size
                if size > MAX_STREAM or stream_bytes > MAX_TOTAL_STREAM:
                    raise ValueError('PDF解壓內容超過處理上限，未匯入。')
            seen = 0
            def visitor(text, *_):
                nonlocal seen
                seen += len(text)
                if used + seen > MAX_CHARS: raise ValueError('PDF文字超過20萬字，未截斷匯入。')
            text = page.extract_text(visitor_text=visitor).replace('\r\n','\n').replace('\r','\n').strip()
            if text: has_text = True
            body = text or '[本頁沒有可抽取文字；可能是圖片或空白，未做OCR]'
            section = f'[PDF 第{number}頁／共{count}頁]\n' + body
            used += len(section) + (2 if output else 0)
            if used > MAX_CHARS: raise ValueError('PDF文字超過20萬字，未截斷匯入。')
            output.append(section)
        if not has_text: raise ValueError('PDF沒有可抽取文字；掃描圖片需先OCR，未匯入空白內容。')
        result = '\n\n'.join(output)
        if any(ord(char) < 32 and char not in '\n\t\f' for char in result):
            raise ValueError('PDF抽取含無效文字，請另存文字副本。')
        return result


def project(raw, *, cancel=None, timeout=TIMEOUT):
    if not isinstance(raw, bytes) or not raw.startswith(b'%PDF-') or len(raw) > MAX_INPUT:
        raise ValueError('PDF格式或大小不符。')
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('PDF解析時限需為正秒數。')
    deadline = time.monotonic() + min(timeout, TIMEOUT)
    def check_running():
        if cancel is not None and cancel.is_set(): raise ValueError('已停止PDF文字讀取。')
        if time.monotonic() >= deadline: raise ValueError('PDF文字讀取逾時，未匯入部分內容。')
    check_running()
    python = Path(sys.executable)
    if python.name.lower() == 'pythonw.exe': python = python.with_name('python.exe')
    # Standard files avoid blocked stdin writes and bound parent allocations.
    with tempfile.TemporaryDirectory(prefix='personal-ai-pdf-') as folder:
        source = Path(folder)/'input.pdf'; destination = Path(folder)/'output.json'
        source.write_bytes(raw)
        with source.open('rb') as stdin, destination.open('wb') as stdout:
            check_running()
            process = subprocess.Popen([str(python), '-m', 'assistant.pdf_text', '--worker'], cwd=ROOT,
                                       stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                while process.poll() is None:
                    check_running()
                    if destination.stat().st_size > MAX_RESULT: raise ValueError('PDF解析回覆過大，未匯入。')
                    time.sleep(.03)
                check_running()
            finally:
                if process.poll() is None:
                    if os.name == 'nt':
                        # The Windows venv launcher may own a child that still holds input.pdf.
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False)
                    if process.poll() is None:
                        process.kill()
                process.wait(timeout=5)
        with destination.open('rb') as stream: result = stream.read(MAX_RESULT+1)
        if len(result)>MAX_RESULT: raise ValueError('PDF解析回覆過大，未匯入。')
        try: value=json.loads(result)
        except (ValueError, UnicodeError): raise ValueError('PDF解析程序未完成或超過記憶體限制，未匯入。') from None
        if isinstance(value,dict) and set(value)=={'error'} and isinstance(value['error'],str):
            raise ValueError('無法讀取PDF：'+value['error'][:1000])
        if (process.returncode != 0 or not isinstance(value,dict) or set(value)!={'content'}
                or not isinstance(value['content'],str) or not 1 <= len(value['content']) <= MAX_CHARS):
            raise ValueError('PDF解析回覆格式不符，未匯入。')
        return value['content']


def main():
    import logging
    logging.disable(logging.CRITICAL)
    try:
        memory_handle = limit_worker_memory()  # Keep the job alive through extraction and output.
        raw = sys.stdin.buffer.read(MAX_INPUT+1)
        value = {'content': extract(raw)}
        code = 0
    except MemoryError:
        value = {'error': 'PDF解析超過512MiB記憶體上限。'}; code = 1
    except Exception as exc:
        value = {'error': str(exc)[:1000] or type(exc).__name__}; code = 1
    sys.stdout.buffer.write(json.dumps(value,ensure_ascii=False).encode('utf-8'))
    raise SystemExit(code)


if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    main()
