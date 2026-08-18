/** Export type definitions. */

export interface QueryResult {
  columns: Array<{ name: string; dataType: string }>;
  rows: Array<Record<string, any>>;
  rowCount: number;
  executionTimeMs: number;
  sql: string;
}

export interface ExportFormat {
  name: string;
  value: string;
  description: string;
}

export interface ExportFormatsResponse {
  formats: ExportFormat[];
  default_format: string;
}

export interface ExportResult {
  success: boolean;
  format: string;
  filename: string;
  row_count: number;
  file_size_bytes: number;
  execution_time_ms: number;
  export_time_ms: number;
  content: string;
}

export interface ExportSuggestion {
  suggested_format: string;
  reason: string;
  sample_row_count: number;
  column_count: number;
  estimated_size: string;
}

export interface ExportHistory {
  id: number;
  database_name: string;
  sql_query: string;
  format: string;
  row_count: number;
  file_size_bytes: number;
  file_name: string;
  status: string;
  created_at: string;
}

export interface ExportAssistantProps {
  queryResult: QueryResult | null;
  onExport: (format: "csv" | "json") => Promise<void>;
  enableAutoSuggest?: boolean;
  databaseName: string | null;
}

export interface ExportPanelProps {
  queryResult: QueryResult | null;
  databaseName: string | null;
  onExecuteAndExport: (format: "csv" | "json") => Promise<void>;
  exportHistory: ExportHistory[];
}