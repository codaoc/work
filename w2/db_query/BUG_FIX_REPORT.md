# 数据库连接错误修复记录

## 🐛 问题描述

用户在添加 MySQL 数据库时遇到 "Failed to add database" 错误。

## 🔍 问题诊断

### 后端错误日志
```
sqlite3.OperationalError: no such column: databaseconnections.db_type
```

### 根本原因

1. **数据库架构不匹配**
   - `DatabaseConnection` 模型定义了 `db_type` 字段
   - 数据库迁移文件缺少该字段
   - 迁移脚本未正确执行

2. **前端问题**
   - URL 验证只允许 `postgresql://`，不支持 `mysql://`
   - 数据类型定义缺少 `dbType` 字段
   - 数据提供器未发送 `dbType` 参数

## 🔧 修复方案

### 1. 数据库迁移修复

**创建新迁移文件**: `002_add_db_type.py`

```python
def upgrade() -> None:
    """Add db_type column to databaseconnections table."""
    with op.batch_alter_table('databaseconnections') as batch_op:
        batch_op.add_column(
            sa.Column('db_type', sa.String(length=20), nullable=True, server_default='postgresql')
        )
```

**执行迁移**:
```bash
cd backend && uv run alembic upgrade head
```

### 2. 前端修复

#### 更新 URL 验证规则
**文件**: `frontend/src/pages/databases/create.tsx`

```tsx
// 修复前
pattern: /^postgresql:\/\//

// 修复后
pattern: /^(postgresql|mysql):\/\//,
message: "URL must start with postgresql:// or mysql://",
```

#### 添加数据库类型选择器
**文件**: `frontend/src/pages/databases/create.tsx`

```tsx
<Form.Item
  label="Database Type"
  name="dbType"
  rules={[{ required: true, message: "Please select database type" }]}
>
  <Select placeholder="Select database type">
    <Select.Option value="postgresql">PostgreSQL</Select.Option>
    <Select.Option value="mysql">MySQL</Select.Option>
  </Select>
</Form.Item>
```

#### 更新类型定义
**文件**: `frontend/src/types/database.ts`

```typescript
export interface DatabaseConnection {
  name: string;
  url: string;
  dbType: "postgresql" | "mysql";  // 新增
  // ...
}

export interface DatabaseConnectionInput {
  url: string;
  dbType?: "postgresql" | "mysql";  // 新增
  description?: string | null;
}
```

#### 更新数据提供器
**文件**: `frontend/src/services/dataProvider.ts`

```typescript
create: async ({ resource, variables }) => {
  if (resource === "databases") {
    const response = await apiClient.put<DatabaseConnection>(
      `/api/v1/dbs/${input.name}`,
      {
        url: input.url,
        description: input.description,
        dbType: input.dbType,  // 新增
      }
    );
    return { data: response.data as any };
  }
}
```

## ✅ 验证结果

### 后端测试
```bash
# 添加 MySQL 数据库
curl -X PUT "http://localhost:8000/api/v1/dbs/MySQL测试库" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "mysql://testuser:testpass123@localhost:3306/testdb",
    "dbType": "mysql",
    "description": "MySQL测试环境"
  }'
```

**结果**: ✅ 成功返回数据库连接信息

```json
{
    "name": "MySQL测试库",
    "url": "mysql://testuser:testpass123@localhost:3306/testdb",
    "dbType": "mysql",
    "description": "MySQL测试环境",
    "createdAt": "2026-08-17T08:27:01.790266",
    "updatedAt": "2026-08-17T08:27:01.790325",
    "lastConnectedAt": "2026-08-17T08:27:01.789607",
    "status": "active"
}
```

## 📋 修改文件清单

| 文件 | 修改内容 |
|------|----------|
| `backend/alembic/versions/002_add_db_type.py` | 新增迁移文件 |
| `frontend/src/pages/databases/create.tsx` | 添加数据库类型选择器，更新 URL 验证 |
| `frontend/src/types/database.ts` | 添加 `dbType` 字段 |
| `frontend/src/services/dataProvider.ts` | 发送 `dbType` 参数 |

## 🎯 使用方法

### 添加 MySQL 数据库

1. 打开 http://localhost:5173
2. 点击 "添加数据库"
3. 填写信息：
   ```
   名称: MySQL测试库
   数据库类型: MySQL
   连接串: mysql://testuser:testpass123@localhost:3306/testdb
   描述: MySQL测试环境
   ```
4. 点击 "保存"

### 或使用 API 测试

```bash
# 添加数据库
curl -X PUT "http://localhost:8000/api/v1/dbs/MySQL测试库" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "mysql://testuser:testpass123@localhost:3306/testdb",
    "dbType": "mysql",
    "description": "MySQL测试环境"
  }'

# 列出所有数据库
curl http://localhost:8000/api/v1/dbs
```

## 🔄 后续建议

1. **自动化类型检测**: 后端已实现 URL 自动检测，前端可以考虑使用该特性
2. **编辑功能**: 添加数据库编辑页面，允许修改连接信息
3. **连接测试**: 在保存前先测试连接，提供即时反馈
4. **更多数据库**: 按架构设计，可轻松添加 Oracle、SQLite 等支持

## 📅 修复时间

- 2026-08-17 16:20 - 问题诊断
- 2026-08-17 16:25 - 数据库迁移修复
- 2026-08-17 16:30 - 前端代码修复
- 2026-08-17 16:35 - 测试验证通过