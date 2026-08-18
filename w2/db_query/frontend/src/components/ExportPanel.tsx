/** Export Panel - Advanced export controls and history. */

import React, { useState } from "react";
import {
  Card,
  Button,
  Space,
  Typography,
  Select,
  Table,
  Tag,
  message,
  Modal,
  Tooltip,
} from "antd";
import {
  FileExcelOutlined,
  FileTextOutlined,
  HistoryOutlined,
  DownloadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { ExportPanelProps, ExportHistory } from "../types/export";
import { apiClient } from "../services/api";

const { Text } = Typography;

export const ExportPanel: React.FC<ExportPanelProps> = ({
  queryResult,
  databaseName,
  onExecuteAndExport,
}) => {
  const [selectedFormat, setSelectedFormat] = useState<"csv" | "json">("csv");
  const [selectedHistory, setSelectedHistory] = useState<ExportHistory[]>([]);
  const [loading, setLoading] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  const handleExport = async () => {
    if (!queryResult || queryResult.rows.length === 0) {
      message.warning("没有数据可导出");
      return;
    }

    setLoading(true);
    try {
      await onExecuteAndExport(selectedFormat);
      message.success(`已导出为 ${selectedFormat.toUpperCase()}`);
    } catch (error) {
      message.error(`导出失败: ${error}`);
    } finally {
      setLoading(false);
    }
  };

  const handleQuickExport = (format: "csv" | "json") => {
    setSelectedFormat(format);
    handleExport();
  };

  const showHistoryModal = () => {
    setShowHistory(true);
    loadHistory();
  };

  const loadHistory = async () => {
    try {
      const response = await apiClient.get("/api/v1/exports/history", {
        params: { database_name: databaseName || "", limit: 20 },
      });
      setSelectedHistory(response.data || []);
    } catch (error) {
      console.error("Failed to load export history:", error);
    }
  };

  const handleDownloadHistory = async (filename: string) => {
    try {
      const response = await apiClient.get(`/api/v1/exports/download/${filename}`, {
        responseType: "blob",
      });

      const url = window.URL.createObjectURL(new Blob([response.data]));
      const link = document.createElement("a");
      link.href = url;
      link.setAttribute("download", filename);
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);

      message.success(`已下载 ${filename}`);
    } catch (error) {
      message.error("下载失败");
    }
  };

  const historyColumns = [
    {
      title: "文件名",
      dataIndex: "file_name",
      key: "file_name",
      ellipsis: true,
    },
    {
      title: "格式",
      dataIndex: "format",
      key: "format",
      width: 80,
      render: (format: string) => (
        <Tag color={format === "csv" ? "green" : "blue"}>{format.toUpperCase()}</Tag>
      ),
    },
    {
      title: "行数",
      dataIndex: "row_count",
      key: "row_count",
      width: 80,
    },
    {
      title: "大小",
      dataIndex: "file_size_bytes",
      key: "file_size_bytes",
      width: 100,
      render: (size: number) => {
        const sizeInKB = (size / 1024).toFixed(2);
        return `${sizeInKB} KB`;
      },
    },
    {
      title: "时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 150,
      render: (date: string) => new Date(date).toLocaleString("zh-CN"),
    },
    {
      title: "操作",
      key: "actions",
      width: 80,
      render: (_: any, record: ExportHistory) => (
        <Space size="small">
          <Tooltip title="下载">
            <Button
              type="text"
              icon={<DownloadOutlined />}
              onClick={() => handleDownloadHistory(record.file_name)}
              size="small"
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  return (
    <>
      <Card
        title={
          <Space size={8}>
            <ThunderboltOutlined style={{ fontSize: 20 }} />
            <span style={{ textTransform: "uppercase", fontWeight: 700 }}>
              高级导出
            </span>
          </Space>
        }
        style={{
          background: "#FFDE00",
          border: "2px solid #000",
          marginBottom: 16,
        }}
        bodyStyle={{ padding: 16 }}
        extra={
          <Button
            icon={<HistoryOutlined />}
            onClick={showHistoryModal}
            style={{
              border: "2px solid #000",
              fontWeight: 600,
            }}
          >
            导出历史
          </Button>
        }
      >
        <Space
          direction="vertical"
          size={16}
          style={{ width: "100%" }}
        >
          {/* Quick export buttons */}
          <div>
            <Text strong style={{ color: "#000", textTransform: "uppercase" }}>
              快速导出
            </Text>
            <div style={{ marginTop: 8 }}>
              <Space size={8}>
                <Button
                  type="primary"
                  icon={<FileExcelOutlined />}
                  onClick={() => handleQuickExport("csv")}
                  disabled={!queryResult || queryResult.rows.length === 0}
                  loading={loading && selectedFormat === "csv"}
                  style={{
                    background: "#52c41a",
                    color: "#FFF",
                    border: "2px solid #000",
                    fontWeight: 600,
                  }}
                >
                  导出 CSV
                </Button>
                <Button
                  type="primary"
                  icon={<FileTextOutlined />}
                  onClick={() => handleQuickExport("json")}
                  disabled={!queryResult || queryResult.rows.length === 0}
                  loading={loading && selectedFormat === "json"}
                  style={{
                    background: "#1890ff",
                    color: "#FFF",
                    border: "2px solid #000",
                    fontWeight: 600,
                  }}
                >
                  导出 JSON
                </Button>
              </Space>
            </div>
          </div>

          {/* Advanced export settings */}
          <div>
            <Text strong style={{ color: "#000", textTransform: "uppercase" }}>
              高级设置
            </Text>
            <div
              style={{
                marginTop: 8,
                background: "#FFF",
                padding: 12,
                border: "2px solid #000",
              }}
            >
              <Space
                direction="vertical"
                size={8}
                style={{ width: "100%" }}
              >
                <div>
                  <Text style={{ color: "#000" }}>导出格式:</Text>
                  <Select
                    value={selectedFormat}
                    onChange={(value: "csv" | "json") => setSelectedFormat(value)}
                    style={{ width: 150, marginLeft: 8 }}
                  >
                    <Select.Option value="csv">
                      <Space size={4}>
                        <FileExcelOutlined />
                        CSV 格式
                      </Space>
                    </Select.Option>
                    <Select.Option value="json">
                      <Space size={4}>
                        <FileTextOutlined />
                        JSON 格式
                      </Space>
                    </Select.Option>
                  </Select>
                </div>

                <div>
                  <Text style={{ color: "#000" }}>当前查询结果:</Text>
                  <div style={{ marginLeft: 8, color: "#000" }}>
                    <Text>
                      {queryResult ? queryResult.rowCount.toLocaleString() : 0} 行
                    </Text>
                    {queryResult && (
                      <Text style={{ marginLeft: 8, color: "#999" }}>
                        ({queryResult.executionTimeMs}ms 执行)
                      </Text>
                    )}
                  </div>
                </div>

                <Button
                  type="primary"
                  icon={<DownloadOutlined />}
                  onClick={handleExport}
                  disabled={!queryResult || queryResult.rows.length === 0}
                  loading={loading}
                  style={{
                    background: "#000",
                    color: "#FFF",
                    border: "2px solid #000",
                    fontWeight: 700,
                    textTransform: "uppercase",
                  }}
                >
                  开始导出
                </Button>
              </Space>
            </div>
          </div>
        </Space>
      </Card>

      {/* Export History Modal */}
      <Modal
        title={
          <Space size={8}>
            <HistoryOutlined />
            <span style={{ textTransform: "uppercase", fontWeight: 700 }}>
              导出历史
            </span>
          </Space>
        }
        open={showHistory}
        onCancel={() => setShowHistory(false)}
        footer={null}
        width={800}
      >
        <Table
          columns={historyColumns}
          dataSource={selectedHistory}
          rowKey="id"
          pagination={{ pageSize: 10 }}
          size="small"
        />
      </Modal>
    </>
  );
};