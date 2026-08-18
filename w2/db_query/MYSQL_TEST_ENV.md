# MySQL 测试环境配置

## 📊 数据库信息

| 配置项 | 值 |
|--------|-----|
| **数据库** | testdb |
| **主机** | localhost |
| **端口** | 3306 |
| **root 密码** | 123123 |

## 👤 用户账户

| 用户名 | 密码 | 权限 |
|--------|------|------|
| testuser | testpass123 | testdb 的所有权限 |

## 🔗 连接字符串

```bash
# Root 用户
mysql://root:123123@localhost:3306/testdb

# 测试用户
mysql://testuser:testpass123@localhost:3306/testdb
```

## 📋 数据库表结构

### users (用户表)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INT (PK) | 用户 ID |
| username | VARCHAR(50) | 用户名 |
| email | VARCHAR(100) | 邮箱 |
| age | INT | 年龄 |
| created_at | TIMESTAMP | 创建时间 |

### products (产品表)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INT (PK) | 产品 ID |
| name | VARCHAR(100) | 产品名称 |
| price | DECIMAL(10,2) | 价格 |
| category | VARCHAR(50) | 类别 |
| stock_quantity | INT | 库存数量 |

### orders (订单表)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INT (PK) | 订单 ID |
| user_id | INT (FK) | 用户 ID |
| product_name | VARCHAR(100) | 产品名称 |
| amount | DECIMAL(10,2) | 订单金额 |
| order_date | TIMESTAMP | 订单日期 |

## 🧪 测试数据

### Users (5条记录)
- alice (28岁) - alice@example.com
- bob (34岁) - bob@example.com
- charlie (45岁) - charlie@example.com
- diana (23岁) - diana@example.com
- eve (31岁) - eve@example.com

### Products (5条记录)
1. Laptop - ¥999.99 (Electronics, 库存50)
2. Mouse - ¥29.99 (Accessories, 库存200)
3. Keyboard - ¥79.99 (Accessories, 库存150)
4. Monitor - ¥349.99 (Electronics, 库存75)
5. Desk - ¥299.99 (Furniture, 库存30)

### Orders (6条记录)
1. alice 购买 Laptop - ¥999.99
2. bob 购买 Mouse - ¥29.99
3. alice 购买 Keyboard - ¥79.99
4. charlie 购买 Monitor - ¥349.99
5. diana 购买 Desk - ¥299.99
6. bob 购买 Mouse - ¥29.99

## 💻 使用示例

### 命令行连接
```bash
# Root 用户
mysql -u root -p'123123' testdb

# 测试用户
mysql -u testuser -p'testpass123' testdb
```

### 快速查询
```sql
-- 查看所有用户
SELECT * FROM users;

-- 查看所有产品
SELECT * FROM products;

-- 查看订单及用户信息
SELECT o.*, u.username FROM orders o JOIN users u ON o.user_id = u.id;

-- 查询订单统计
SELECT u.username, COUNT(o.id) as order_count, SUM(o.amount) as total_spent
FROM users u LEFT JOIN orders o ON u.id = o.user_id
GROUP BY u.id, u.username;
```

## 🎯 在项目中使用

### 添加数据库连接
1. 打开 http://localhost:5173
2. 点击 "添加数据库"
3. 填写信息：
   - 名称: `MySQL测试库`
   - 类型: `MySQL`
   - 连接串: `mysql://testuser:testpass123@localhost:3306/testdb`

### 测试查询
```sql
-- 简单查询
SELECT * FROM users LIMIT 5;

-- 聚合查询
SELECT category, COUNT(*) as count, AVG(price) as avg_price
FROM products
GROUP BY category;

-- 复杂查询
SELECT u.username, p.name, o.amount, o.order_date
FROM orders o
JOIN users u ON o.user_id = u.id
JOIN products p ON o.product_name = p.name
ORDER BY o.order_date DESC;
```

## 🔧 管理

### 启动/停止服务
```bash
# 启动
sudo systemctl start mysqld

# 停止
sudo systemctl stop mysqld

# 重启
sudo systemctl restart mysqld

# 查看状态
sudo systemctl status mysqld
```

### 重置密码
```bash
sudo mysql -u root -e "ALTER USER 'testuser'@'%' IDENTIFIED BY '新密码';"
```

## 📝 注意事项

1. 端口 3306 默认只监听 localhost
2. 如需远程访问，需修改 `/etc/my.cnf.d/mariadb-server.cnf` 配置
3. 测试数据仅用于开发环境，生产环境请勿使用简单密码