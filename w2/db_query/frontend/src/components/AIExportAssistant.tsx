/** AI Export Assistant - Smart suggestions for exporting query results. */

import React, { useState, useEffect } from "react";
import {
  Card,
  Button,
  Space,
  Typography,
  message,
  Spin,
} from "antd";
import {
  FileExcelOutlined,
  FileTextOutlined,
  CloseOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { ExportAssistantProps } from "../types/export";
import { apiClient } from "../services/api";

const { Title, Text } = Typography;

export const AIExportAssistant: React.FC<ExportAssistantProps> = ({
  queryResult,
  onExport,
  enableAutoSuggest = true,
  databaseName,
}) => {
  const [visible, setVisible] = useState(false);
  const [suggestion, setSuggestion] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  // Show suggestion when query completes
  useEffect(() => {
    if (
      queryResult &&
      queryResult.rows.length > 0 &&
      enableAutoSuggest &&
      !dismissed
    ) {
      loadSuggestion();
    }
  }, [queryResult, enableAutoSuggest, dismissed]);

  const loadSuggestion = async () => {
    if (!databaseName || !queryResult) return;

    setLoading(true);
    try {
      const response = await apiClient.post("/api/v1/exports/suggest", {
        database_name: databaseName,
        sql: queryResult.sql,
      });
      setSuggestion(response.data);
      setVisible(true);
    } catch (error) {
      console.error("Failed to get export suggestion:", error);
      // Still show suggestion even if API fails
      setSuggestion({
        suggested_format: "csv",
        reason: "Recommended for tabular data",
      });
      setVisible(true);
    } finally {
      setLoading(false);
    }
  };

  const handleExport = async (format: "csv" | "json") => {
    try {
      await onExport(format);
      message.success(`Exported to ${format.toUpperCase()}`);
      setVisible(false);
    } catch (error) {
      message.error(`Export failed: ${error}`);
    }
  };

  const handleDismiss = () => {
    setVisible(false);
    setDismissed(true);
  };

  const handleExecuteAndExport = async (format: "csv" | "json") => {
    if (!databaseName || !queryResult) return;

    try {
      setLoading(true);
      const response = await apiClient.post("/api/v1/exports/query", {
        database_name: databaseName,
        sql: queryResult.sql,
        format: format,
      });

      if (response.data.success) {
        // Download the file
        downloadFile(response.data.content, response.data.filename, format);
        message.success(
          `Query executed and exported: ${response.data.row_count} rows to ${format.toUpperCase()}`
        );
        setVisible(false);
      } else {
        message.error(`Export failed: ${response.data.error || "Unknown error"}`);
      }
    } catch (error: any) {
      message.error(
        error.response?.data?.detail || "Execute and export failed"
      );
    } finally {
      setLoading(false);
    }
  };

  const downloadFile = (content: string, filename: string, format: string) => {
    const mimeType = format === "csv" ? "text/csv" : "application/json";
    const blob = new Blob([content], { type: mimeType + ";charset=utf-8;" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    link.click();
    URL.revokeObjectURL(link.href);
  };

  if (!visible || !queryResult) {
    return null;
  }

  const suggestedFormat = suggestion?.suggested_format || "csv";
  const formatName = suggestedFormat === "csv" ? "CSV" : "JSON";
  const formatIcon =
    suggestedFormat === "csv" ? <FileExcelOutlined /> : <FileTextOutlined />;

  return (
    <Card
      style={{
        background: "#FFDE00",
        border: "2px solid #000",
        marginBottom: 16,
      }}
      bodyStyle={{ padding: 16 }}
    >
      <Space
        direction="vertical"
        size={12}
        style={{ width: "100%" }}
      >
        {/* Header */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
          }}
        >
          <Space size={8}>
            <ThunderboltOutlined style={{ fontSize: 20, color: "#000" }} />
            <Title
              level={5}
              style={{
                margin: 0,
                textTransform: "uppercase",
                fontWeight: 700,
                letterSpacing: "0.04em",
                color: "#000",
              }}
            >
              智能导出建议
            </Title>
          </Space>
          <Button
            type="text"
            icon={<CloseOutlined />}
            onClick={handleDismiss}
            style={{ color: "#000" }}
          />
        </div>

        {/* Query summary */}
        <div style={{ background: "#FFF", padding: 12, border: "2px solid #000" }}>
          <Text strong style={{ color: "#000" }}>
            查询完成！
          </Text>
          <Text style={{ color: "#000", marginLeft: 8 }}>
            发现{" "}
            <Text strong style={{ color: "#000" }}>
              {queryResult.rowCount.toLocaleString()}
            </Text>{" "}
            条数据，
            {queryResult.executionTimeMs}ms 执行完成。
          </Text>
        </div>

        {/* Suggestion */}
        {loading ? (
          <div style={{ textAlign: "center", padding: 8 }}>
            <Spin size="small" />
          </div>
        ) : (
          <div style={{ background: "#FFF", padding: 12, border: "2px solid #000" }}>
            <Space size={8}>
              {formatIcon}
              <Text strong style={{ color: "#000" }}>
                建议导出格式: {formatName}
              </Text>
            </Space>
            <div style={{ marginTop: 8 }}>
              <Text style={{ color: "#000" }}>
                {suggestion?.reason || "最适合当前数据类型"}
              </Text>
            </div>
          </div>
        )}

        {/* Action buttons */}
        <Space
          size={12}
          style={{ width: "100%", justifyContent: "space-between" }}
        >
          <Space size={8}>
            <Button
              type="primary"
              icon={<FileExcelOutlined />}
              onClick={() => handleExport("csv")}
              style={{
                background: suggestedFormat === "csv" ? "#000" : "#FFF",
                color: suggestedFormat === "csv" ? "#FFF" : "#000",
                border: "2px solid #000",
                fontWeight: 600,
              }}
            >
              导出 CSV
            </Button>
            <Button
              type="primary"
              icon={<FileTextOutlined />}
              onClick={() => handleExport("json")}
              style={{
                background: suggestedFormat === "json" ? "#000" : "#FFF",
                color: suggestedFormat === "json" ? "#FFF" : "#000",
                border: "2px solid #000",
                fontWeight: 600,
              }}
            >
              导出 JSON
            </Button>
          </Space>

          <Button
            type="default"
            onClick={() => handleExecuteAndExport(suggestedFormat as "csv" | "json")}
            style={{
              border: "2px solid #000",
              fontWeight: 600,
            }}
          >
            重新执行并导出 {formatName}
          </Button>
        </Space>
      </Space>
    </Card>
  );
};