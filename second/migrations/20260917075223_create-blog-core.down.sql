-- migration: create-blog-core
-- request: 博客核心表：文章
-- 由 pg-agent 生成；事务由 runner 包裹，本文件保持纯 SQL 可被标准工具重放

DROP TABLE posts;
