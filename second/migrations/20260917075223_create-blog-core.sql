-- migration: create-blog-core
-- request: 博客核心表：文章
-- 由 pg-agent 生成；事务由 runner 包裹，本文件保持纯 SQL 可被标准工具重放

CREATE TABLE posts (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  author_id bigint NOT NULL,
  title text NOT NULL,
  body text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_posts_author ON posts(author_id);
;
