"""Export API endpoints for query result exports."""

from fastapi import APIRouter, HTTPException, status, Response, Depends
from typing import Optional
import logging

from app.services.export_service import export_service
from app.services.database_service import database_service
from app.models.database import DatabaseType
from app.models.export_history import ExportFormat, ExportStatus
from sqlmodel import Session, select
from app.database import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])


@router.get("/formats")
async def get_supported_formats() -> dict:
    """Get supported export formats.

    Returns:
        Dictionary of supported formats
    """
    return {
        "formats": [
            {"name": "CSV", "value": "csv", "description": "Comma-separated values"},
            {"name": "JSON", "value": "json", "description": "JSON array format"},
        ],
        "default_format": "csv",
    }


@router.post("/query")
async def execute_and_export(
    request_data: dict,
    session: Session = Depends(get_session),
) -> dict:
    """Execute query and export results to specified format.

    Args:
        request_data: Request data with database_name, sql, and format
        session: Database session

    Returns:
        Export result with file information
    """
    try:
        # Extract parameters
        database_name = request_data.get("database_name")
        sql = request_data.get("sql")
        format = request_data.get("format", "csv")

        # Validate parameters
        if not database_name or not sql:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="database_name and sql are required",
            )
        # Validate format
        if format not in ["csv", "json"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported format: {format}. Supported formats: csv, json",
            )

        # Get database connection info
        from app.models.database import DatabaseConnection
        statement = select(DatabaseConnection).where(
            DatabaseConnection.name == database_name
        )
        connection = session.exec(statement).first()

        if not connection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Database connection '{database_name}' not found",
            )

        # Execute query
        query_result, execution_time_ms = await database_service.execute_query(
            db_type=connection.db_type,
            name=database_name,
            url=connection.url,
            sql=sql,
            limit=10000,
        )

        # Convert to dict
        query_result_dict = {
            "columns": [{"name": col["name"], "dataType": col["dataType"]} for col in query_result.columns],
            "rows": query_result.rows,
            "rowCount": query_result.row_count,
            "executionTimeMs": execution_time_ms,
            "sql": sql,
        }

        # Export to specified format
        if format == "csv":
            export_result = export_service.export_to_csv(query_result_dict, database_name)
        else:
            export_result = export_service.export_to_json(query_result_dict, database_name)

        if not export_result["success"]:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Export failed: {export_result.get('error', 'Unknown error')}",
            )

        # Return export result with content for download
        return {
            "success": True,
            "format": export_result["format"],
            "filename": export_result["filename"],
            "row_count": export_result["row_count"],
            "file_size_bytes": export_result["file_size_bytes"],
            "execution_time_ms": execution_time_ms,
            "export_time_ms": export_result["export_time_ms"],
            "content": export_result["content"],  # Include content for client download
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Execute and export failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Execute and export failed: {str(e)}",
        )


@router.post("/suggest")
async def suggest_export_format(
    request_data: dict,
    session: Session = Depends(get_session),
) -> dict:
    """Suggest optimal export format based on query results.

    Args:
        request_data: Request data with database_name and sql
        session: Database session

    Returns:
        Suggested format with reasoning
    """
    try:
        database_name = request_data.get("database_name")
        sql = request_data.get("sql")

        if not database_name or not sql:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="database_name and sql are required",
            )
        # Get database connection info
        from app.models.database import DatabaseConnection
        statement = select(DatabaseConnection).where(
            DatabaseConnection.name == database_name
        )
        connection = session.exec(statement).first()

        if not connection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Database connection '{database_name}' not found",
            )

        # Execute query with small limit for analysis
        query_result, _ = await database_service.execute_query(
            db_type=connection.db_type,
            name=database_name,
            url=connection.url,
            sql=sql,
            limit=100,  # Small limit for format analysis
        )

        # Analyze and suggest format
        query_result_dict = {
            "columns": [{"name": col["name"], "dataType": col["dataType"]} for col in query_result.columns],
            "rows": query_result.rows,
            "rowCount": query_result.row_count,
            "executionTimeMs": 0,
            "sql": sql,
        }

        export_result = export_service.export_with_auto_format(
            query_result_dict, database_name, auto_format=None
        )

        return {
            "suggested_format": export_result["suggested_format"],
            "reason": export_result["suggestion_reason"],
            "sample_row_count": query_result.row_count,
            "column_count": len(query_result.columns),
            "estimated_size": f"~{export_result.get('file_size_bytes', 0) * 10} bytes",  # Rough estimate
        }

    except Exception as e:
        logger.error(f"Format suggestion failed: {e}")
        return {
            "suggested_format": "csv",
            "reason": "Default (analysis failed)",
            "error": str(e),
        }


@router.get("/download/{filename}")
async def download_export(filename: str) -> Response:
    """Download exported file.

    Args:
        filename: Name of the export file

    Returns:
        File response with appropriate content type
    """
    try:
        import os
        from pathlib import Path

        file_path = export_service.export_dir / filename

        if not file_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Export file '{filename}' not found",
            )

        # Determine content type
        content_type = "text/csv" if filename.endswith(".csv") else "application/json"

        # Read file content
        with open(file_path, "rb") as f:
            content = f.read()

        return Response(
            content=content,
            media_type=content_type,
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download export failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Download failed: {str(e)}",
        )


@router.get("/history")
async def get_export_history(
    database_name: Optional[str] = None,
    limit: int = 50,
    session: Session = Depends(get_session),
) -> list:
    """Get export history.

    Args:
        database_name: Filter by database name
        limit: Maximum number of records
        session: Database session

    Returns:
        List of export history records
    """
    try:
        from app.models.export_history import ExportHistory

        query = select(ExportHistory)

        if database_name:
            query = query.where(ExportHistory.database_name == database_name)

        query = query.order_by(ExportHistory.created_at.desc()).limit(limit)

        results = session.exec(query).all()
        return results

    except Exception as e:
        logger.error(f"Failed to get export history: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get export history: {str(e)}",
        )


@router.delete("/history")
async def clear_export_history(
    database_name: Optional[str] = None,
    session: Session = Depends(get_session),
) -> dict:
    """Clear export history.

    Args:
        database_name: Filter by database name, None for all
        session: Database session

    Returns:
        Deletion result
    """
    try:
        from app.models.export_history import ExportHistory

        query = session.exec(select(ExportHistory))

        if database_name:
            query = query.where(ExportHistory.database_name == database_name)

        results = query.all()
        count = len(results)

        for record in results:
            session.delete(record)

        session.commit()

        logger.info(f"Cleared {count} export history records")

        return {"success": True, "deleted_count": count}

    except Exception as e:
        session.rollback()
        logger.error(f"Failed to clear export history: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clear export history: {str(e)}",
        )