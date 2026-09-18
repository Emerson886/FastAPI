"""
==============================================================================
 文档加载与切片服务
==============================================================================

RAG 的第一步：把用户上传的各类文件变成「纯文本」，再切成「语义片段」。

支持的格式（在 .env 的 ALLOWED_UPLOAD_EXTENSIONS 里配置）：
    .txt / .md   —— 直接按文本读
    .csv         —— 逐行转成文本
    .pdf         —— pypdf 解析
    .docx        —— python-docx 解析

【切片策略（chunk）为什么重要？】
    切片太大：一条切片包含太多无关内容，检索精度下降，且浪费 token
    切片太小：语义被打断，大模型拿到的上下文不完整，回答质量差
    经验值：中文 400~800 字符，重叠 10%~20%（本项目默认 600/100，可在 .env 调整）

    本模块使用 LangChain 的 RecursiveCharacterTextSplitter，
    它会优先在「段落 → 换行 → 句号 → 逗号」这些自然边界切分，
    比固定长度截断效果好得多。

【切片元数据（metadata）】
    每个切片都会带上：
        document_id / owner_id / is_public / filename / category / chunk_index
    其中 owner_id + is_public 是【权限过滤的关键】，检索时用来排除无权访问的内容。
"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.logging_config import logger
from app.core.response import BusinessCode
from app.models.document import Document


# =============================================================================
# 1. 文件读取（把二进制文件转成纯文本）
# =============================================================================
def calculate_file_hash(content: bytes) -> str:
    """
    计算文件内容的 SHA256。

    用途：去重（同一份文件重复上传时直接复用，不重复消耗 Embedding 费用）。
    """
    return hashlib.sha256(content).hexdigest()


def read_txt(content: bytes) -> str:
    """
    读取纯文本。

    编码处理策略：先按 UTF-8 解码，失败则尝试 GBK（Windows 中文用户常见），
    最后兜底用 errors="ignore" 强制解码，保证不因为个别乱码字符导致整个文件解析失败。
    """
    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    logger.warning("文本编码识别失败，使用 utf-8 + ignore 强制解码")
    return content.decode("utf-8", errors="ignore")


def read_csv(content: bytes) -> str:
    """
    读取 CSV：把每行拼成「列名: 值」的自然语言文本。

    为什么要转换格式？直接把 CSV 原文喂给大模型，它需要自己理解表头对应关系；
    转成「姓名: 张三, 部门: 技术部」这种形式后，检索和回答都更准确。
    """
    text = read_txt(content)
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return ""

    header = rows[0]
    lines: list[str] = []
    for row in rows[1:]:
        # 用字典把表头和数据对应起来
        pairs = [f"{header[i].strip()}: {row[i].strip()}"
                 for i in range(min(len(header), len(row)))
                 if row[i].strip()]
        if pairs:
            lines.append(" | ".join(pairs))
    return "\n".join(lines)


def read_pdf(content: bytes) -> str:
    """
    读取 PDF 文本内容。

    依赖：pip install pypdf
    说明：扫描版 PDF（图片）无法提取文字，需要 OCR（如 paddleocr），
         本项目未集成 OCR，遇到扫描件会提示用户。
    """
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        raise BusinessException(
            message="缺少 pypdf 依赖，请执行：pip install pypdf", code=BusinessCode.DOC_PARSE_ERROR
        )

    try:
        reader = PdfReader(io.BytesIO(content))
        pages: list[str] = []
        for index, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if page_text.strip():
                # 保留页码信息，便于引用时定位
                pages.append(f"[第 {index + 1} 页]\n{page_text}")
        text = "\n\n".join(pages)
        if not text.strip():
            raise BusinessException(
                message="未能从 PDF 中提取到文字，可能是扫描版文件（需要 OCR）",
                code=BusinessCode.DOC_PARSE_ERROR,
            )
        return text
    except BusinessException:
        raise
    except Exception as exc:
        logger.exception("PDF 解析失败")
        raise BusinessException(
            message=f"PDF 解析失败：{exc}", code=BusinessCode.DOC_PARSE_ERROR
        )


def read_docx(content: bytes) -> str:
    """
    读取 Word 文档（.docx）。

    依赖：pip install python-docx
    注意：只能处理 .docx（2007+ 格式），老的 .doc 需要先转换格式。
    """
    try:
        import docx
    except ImportError:  # pragma: no cover
        raise BusinessException(
            message="缺少 python-docx 依赖，请执行：pip install python-docx",
            code=BusinessCode.DOC_PARSE_ERROR,
        )

    try:
        document = docx.Document(io.BytesIO(content))
        parts: list[str] = [p.text for p in document.paragraphs if p.text.strip()]

        # 表格内容也要提取（企业文档里表格常常含关键信息，如报销标准）
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))

        return "\n".join(parts)
    except Exception as exc:
        logger.exception("Word 解析失败")
        raise BusinessException(
            message=f"Word 解析失败：{exc}", code=BusinessCode.DOC_PARSE_ERROR
        )


# 后缀 → 解析函数 的映射表（新增格式只需在这里加一行）
PARSERS = {
    ".txt": read_txt,
    ".md": read_txt,
    ".csv": read_csv,
    ".pdf": read_pdf,
    ".docx": read_docx,
}


def parse_file(filename: str, content: bytes) -> str:
    """
    统一入口：根据文件后缀选择解析器。

    :raises BusinessException: 格式不支持或解析失败
    """
    ext = Path(filename).suffix.lower()
    parser = PARSERS.get(ext)
    if parser is None:
        raise BusinessException(
            message=f"暂不支持的文件格式：{ext}，支持：{', '.join(PARSERS.keys())}",
            code=BusinessCode.FILE_TYPE_NOT_ALLOWED,
        )

    logger.info("开始解析文件 | {} | 大小={} 字节", filename, len(content))
    text = parser(content)

    # 清理多余空白，避免切片里出现大量空行
    text = "\n".join(line.rstrip() for line in text.splitlines())
    cleaned = "\n".join(line for line in text.split("\n") if line.strip())

    logger.info("文件解析完成 | {} | 提取字符数={}", filename, len(cleaned))
    return cleaned


# =============================================================================
# 2. 文本切片
# =============================================================================
def get_text_splitter() -> RecursiveCharacterTextSplitter:
    """
    创建文本切片器。

    参数来自 .env（RAG_CHUNK_SIZE / RAG_CHUNK_OVERLAP），可随时调整而无需改代码。

    separators 顺序很重要：LangChain 会按顺序尝试，
    优先在「段落」处切分，实在太长才退化为按句号、逗号切分。
    """
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.RAG_CHUNK_SIZE,
        chunk_overlap=settings.RAG_CHUNK_OVERLAP,
        length_function=len,
        separators=[
            "\n\n",   # 段落
            "\n",     # 换行
            "。", "！", "？",  # 中文句号
            ". ", "! ", "? ",  # 英文句号
            "；", ";",         # 分号
            "，", ",",         # 逗号
            " ",               # 空格
            "",                # 兜底：硬切
        ],
        keep_separator=True,
    )


def split_text(text: str) -> list[str]:
    """把长文本切成若干片段，自动过滤空白片段。"""
    if not text.strip():
        return []
    chunks = get_text_splitter().split_text(text)
    return [c.strip() for c in chunks if c.strip()]


def build_chunk_metadatas(
    document: Document,
    chunk_count: int,
) -> list[dict[str, Any]]:
    """
    为每个切片生成元数据。

    ⚠ 【安全关键】owner_id 与 is_public 必须写入！
        它们会在向量检索时用于过滤，确保用户只能检索到自己有权限的文档。
        少了这两个字段，A 用户提问时可能召回 B 用户的私有文档 —— 严重越权漏洞。
    """
    return [
        {
            # ---- 权限字段（必须有）----
            "document_id": int(document.id),
            "owner_id": int(document.owner_id),
            "is_public": bool(document.is_public),
            # ---- 展示字段（引用来源展示用）----
            "filename": document.filename,
            "title": document.title or document.filename,
            "category": document.category or "",
            "chunk_index": index,
        }
        for index in range(chunk_count)
    ]


def process_document_content(
    document: Document,
    content: bytes,
) -> tuple[list[str], list[dict[str, Any]], str]:
    """
    完整处理流程：解析 → 切片 → 生成元数据。

    返回 (切片文本列表, 切片元数据列表, 提取的全文)
    供上传服务一次性拿到所有需要的数据。
    """
    text = parse_file(document.filename, content)
    chunks = split_text(text)
    metadatas = build_chunk_metadatas(document, len(chunks))
    return chunks, metadatas, text
