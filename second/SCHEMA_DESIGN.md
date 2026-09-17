# Schema 设计日志

> 本文件由 pg-agent 维护，记录每次结构变更及设计决策。可人工修订，修订后 agent 将在新会话中采用人工版本。

## 20260917075223 create-blog-core

- 时间：2026-09-17 15:52
- 风险：🟡 中危
- 涉及表：`posts`
- 需求：博客核心表：文章
- 耗时：8 ms

```sql
    CREATE TABLE posts (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      author_id bigint NOT NULL,
      title text NOT NULL,
      body text,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX idx_posts_author ON posts(author_id);
```
