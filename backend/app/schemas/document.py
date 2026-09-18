"""
==============================================================================
 Pydantic Schema：知识库文档（Document）
==============================================================================

包含：
    - DocumentOut        文档输出（列表/详情）
    - DocumentUploadOut  上传成功后的返回
    - ChunkOut           切片输出（管理端预览）
    - DocumentStatsOut   知识库统计
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DocumentBase(BaseModel):
    """文档可编辑字段。"""

    title: str | None = Field(default=None, max_length=255, description="文档标题")
    category: str | None = Field(default=None, max_length=50, description="分类，如『人事制度』")
    tags: list[str] | None = Field(default=None, description="标签列表")
    description: str | None = Field(default=None, description="文档说明")
    is_public: bool = Field(default=False, description="是否全员可见")


class DocumentCreate(DocumentBase):
    """创建文档记录（由上传服务内部调用，前端不直接访问）。"""

    owner_id: int
    filename: str
    stored_path: str
    file_ext: str
    file_size: int
    file_hash: str | None = None


class DocumentUpdate(BaseModel):
    """更新文档元信息（改标题、分类、可见性等）。"""

    title: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=50)
    tags: list[str] | None = None
    description: str | None = None
    is_public: bool | None = None


class DocumentOut(BaseModel):
    """文档输出结构。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_id: int
    filename: str
    file_ext: str
    file_size: int
    title: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    description: str | None = None
    is_public: bool = False
    status: str = "pending"
    chunk_count: int = 0
    char_count: int = 0
    error_message: str | None = None
    processed_at: datetime | None = None
    created_at: datetime


class DocumentUploadOut(BaseModel):
    """上传接口的返回体。"""

    document: DocumentOut = Field(..., description="新建的文档记录")
    duplicated: bool = Field(
        default=False, description="是否为重复文件（内容哈希相同，已复用已有文档）"
    )
    message: str = Field(default="上传成功", description="提示信息")


class ChunkOut(BaseModel):
    """切片输出（管理端查看切片效果，用于调优 chunk_size）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    chunk_index: int
    content: str
    char_count: int
    vector_id: str | None = None
    created_at: datetime


class DocumentStatsOut(BaseModel):
    """知识库统计卡片数据。"""

    total: int = Field(default=0, description="文档总数")
    completed: int = Field(default=0, description="处理完成数")
    processing: int = Field(default=0, description="处理中数量")
    failed: int = Field(default=0, description="处理失败数")
    chunks: int = Field(default=0, description="切片总数")


class CategoryStatOut(BaseModel):
    """分类统计。"""

    category: str
    count: int
