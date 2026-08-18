"""Export service for handling query result exports."""

import time
import csv
import json
import io
from typing import Dict, Any, Optional
from pathlib import Path
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class ExportService:
    """Service for exporting query results to various formats."""

    def __init__(self, export_dir: str = "~/.db_query/exports"):
        """Initialize export service.

        Args:
            export_dir: Directory to store export files
        """
        self.export_dir = Path(export_dir).expanduser()
        self.export_dir.mkdir(parents=True, exist_ok=True)

    def export_to_csv(
        self,
        query_result: Dict[str, Any],
        database_name: str,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Export query result to CSV format.

        Args:
            query_result: Query result data
            database_name: Name of the database
            filename: Optional custom filename

        Returns:
            Export result with file info
        """
        start_time = time.time()

        try:
            # Extract data
            columns = [col["name"] for col in query_result["columns"]]
            rows = query_result["rows"]
            row_count = len(rows)

            # Create CSV content
            csv_buffer = io.StringIO()
            csv_writer = csv.writer(csv_buffer)

            # Write header
            csv_writer.writerow(columns)

            # Write data rows
            for row in rows:
                csv_row = []
                for col in columns:
                    value = row.get(col)
                    if value is None:
                        csv_row.append("")
                    elif isinstance(value, (dict, list)):
                        csv_row.append(json.dumps(value, ensure_ascii=False))
                    else:
                        csv_row.append(str(value))
                csv_writer.writerow(csv_row)

            # Generate filename
            if not filename:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{database_name}_{timestamp}.csv"

            # Save to file
            file_path = self.export_dir / filename
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                f.write(csv_buffer.getvalue())

            file_size = file_path.stat().st_size
            export_time_ms = int((time.time() - start_time) * 1000)

            logger.info(
                f"Exported {row_count} rows to CSV: {filename} "
                f"({file_size} bytes, {export_time_ms}ms)"
            )

            return {
                "success": True,
                "format": "csv",
                "filename": filename,
                "file_path": str(file_path),
                "file_size_bytes": file_size,
                "row_count": row_count,
                "export_time_ms": export_time_ms,
                "content": csv_buffer.getvalue(),
            }

        except Exception as e:
            export_time_ms = int((time.time() - start_time) * 1000)
            logger.error(f"CSV export failed: {e}")
            return {
                "success": False,
                "format": "csv",
                "error": str(e),
                "export_time_ms": export_time_ms,
            }

    def export_to_json(
        self,
        query_result: Dict[str, Any],
        database_name: str,
        filename: Optional[str] = None,
        pretty: bool = True,
    ) -> Dict[str, Any]:
        """Export query result to JSON format.

        Args:
            query_result: Query result data
            database_name: Name of the database
            filename: Optional custom filename
            pretty: Whether to format JSON with indentation

        Returns:
            Export result with file info
        """
        start_time = time.time()

        try:
            # Extract data
            rows = query_result["rows"]
            row_count = len(rows)

            # Create JSON content
            indent = 2 if pretty else None
            json_content = json.dumps(rows, indent=indent, ensure_ascii=False, default=str)

            # Generate filename
            if not filename:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"{database_name}_{timestamp}.json"

            # Save to file
            file_path = self.export_dir / filename
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(json_content)

            file_size = file_path.stat().st_size
            export_time_ms = int((time.time() - start_time) * 1000)

            logger.info(
                f"Exported {row_count} rows to JSON: {filename} "
                f"({file_size} bytes, {export_time_ms}ms)"
            )

            return {
                "success": True,
                "format": "json",
                "filename": filename,
                "file_path": str(file_path),
                "file_size_bytes": file_size,
                "row_count": row_count,
                "export_time_ms": export_time_ms,
                "content": json_content,
            }

        except Exception as e:
            export_time_ms = int((time.time() - start_time) * 1000)
            logger.error(f"JSON export failed: {e}")
            return {
                "success": False,
                "format": "json",
                "error": str(e),
                "export_time_ms": export_time_ms,
            }

    def export_with_auto_format(
        self,
        query_result: Dict[str, Any],
        database_name: str,
        auto_format: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Auto-export with format detection or suggestion.

        Args:
            query_result: Query result data
            database_name: Name of the database
            auto_format: Force specific format ('csv' or 'json')

        Returns:
            Export result with suggested format
        """
        row_count = len(query_result["rows"])
        column_count = len(query_result["columns"])

        # Auto-detect optimal format
        if not auto_format:
            if row_count > 1000 and column_count > 10:
                # Large dataset with many columns - suggest CSV
                suggested_format = "csv"
                reason = "Large dataset with many columns"
            elif any(
                "json" in str(row).lower() or "dict" in str(type(row)).lower()
                for row in query_result["rows"]
                for val in row.values()
            ):
                # Contains nested data - suggest JSON
                suggested_format = "json"
                reason = "Contains nested/complex data"
            else:
                # Default to CSV
                suggested_format = "csv"
                reason = "Default format"
        else:
            suggested_format = auto_format
            reason = "User specified"

        if suggested_format == "csv":
            result = self.export_to_csv(query_result, database_name)
        else:
            result = self.export_to_json(query_result, database_name)

        result["suggested_format"] = suggested_format
        result["suggestion_reason"] = reason

        return result

    def get_export_history(self, database_name: Optional[str] = None, limit: int = 50) -> list:
        """Get export history from database.

        Args:
            database_name: Filter by database name
            limit: Maximum number of records to return

        Returns:
            List of export history records
        """
        # This would query the database for export history
        # For now, return empty list
        return []

    def cleanup_old_exports(self, days: int = 7) -> int:
        """Clean up export files older than specified days.

        Args:
            days: Number of days to keep exports

        Returns:
            Number of files cleaned up
        """
        import time

        cutoff_time = time.time() - (days * 24 * 60 * 60)
        cleaned_count = 0

        for file_path in self.export_dir.glob("*"):
            if file_path.is_file() and file_path.stat().st_mtime < cutoff_time:
                file_path.unlink()
                cleaned_count += 1
                logger.info(f"Cleaned up old export: {file_path.name}")

        logger.info(f"Cleaned up {cleaned_count} old export files")
        return cleaned_count


# Global export service instance
export_service = ExportService()