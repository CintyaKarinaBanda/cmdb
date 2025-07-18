from botocore.exceptions import ClientError
from datetime import datetime
from services.utils import create_aws_client, get_db_connection, log_change

def get_query_changed_by(query_id, update_date):
    """Busca el usuario que realizó el cambio más cercano a la fecha de actualización"""
    conn = get_db_connection()
    if not conn:
        return "unknown"
    
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT user_name FROM cloudtrail_events
                WHERE resource_type = 'ATHENA' AND resource_name = %s 
                AND ABS(EXTRACT(EPOCH FROM (event_time - %s))) < 86400
                ORDER BY ABS(EXTRACT(EPOCH FROM (event_time - %s))) ASC LIMIT 1
            """, (query_id, update_date, update_date))
            
            if result := cursor.fetchone():
                return result[0]
            return "unknown"
    except Exception as e:
        pass
        return "unknown"
    finally:
        conn.close()

def extract_query_data(query_execution, athena_client, account_name, account_id, region):
    """Extrae datos relevantes de una consulta de Athena"""
    query_id = query_execution["QueryExecutionId"]
    
    # Obtener detalles de la consulta
    query_string = query_execution.get("Query", "")
    database = query_execution.get("QueryExecutionContext", {}).get("Database", "N/A")
    
    # Extraer tablas utilizadas (simplificado)
    tables_used = []
    if "FROM" in query_string.upper():
        parts = query_string.upper().split("FROM")
        if len(parts) > 1:
            table_part = parts[1].split()[0].strip()
            tables_used.append(table_part)
    
    # Información de ejecución
    stats = query_execution.get("Statistics", {})
    execution_time = stats.get("TotalExecutionTimeInMillis", 0) / 1000  # Convertir a segundos
    
    # Estado y propietario
    status = query_execution.get("Status", {})
    state = status.get("State", "UNKNOWN")
    
    # Propietario (usuario que ejecutó la consulta)
    owner = query_execution.get("WorkGroup", "primary")
    
    return {
        "AccountName": account_name[:255],
        "AccountID": account_id[:20],
        "QueryId": query_id[:255],
        "QueryName": f"Query-{query_id[:8]}"[:255],  # Nombre simplificado
        "Domain": database[:255],
        "Description": query_string[:500] if query_string else "N/A",
        "Database": database[:255],
        "TablesUsed": ", ".join(tables_used)[:500] if tables_used else "N/A",
        "ExecutionDuration": execution_time,
        "ExecutionFrequency": "On-demand"[:100],  # Athena es on-demand por defecto
        "Owner": owner[:255],
        "Region": region[:50]
    }

def get_athena_queries(region, credentials, account_id, account_name):
    """Obtiene consultas de Athena de una región."""
    athena_client = create_aws_client("athena", region, credentials)
    if not athena_client:
        return []

    try:
        # Obtener consultas recientes (últimas 50)
        response = athena_client.list_query_executions(MaxResults=50)
        query_ids = response.get("QueryExecutionIds", [])
        
        queries_info = []
        
        # Obtener detalles de cada consulta
        for query_id in query_ids:
            try:
                query_details = athena_client.get_query_execution(QueryExecutionId=query_id)
                query_execution = query_details["QueryExecution"]
                info = extract_query_data(query_execution, athena_client, account_name, account_id, region)
                queries_info.append(info)
            except Exception:
                continue
        
        return queries_info
    except ClientError as e:
        pass
        return []

def insert_or_update_athena_data(athena_data):
    """Inserta o actualiza datos de Athena en la base de datos con seguimiento de cambios."""
    if not athena_data:
        return {"processed": 0, "inserted": 0, "updated": 0}

    conn = get_db_connection()
    if not conn:
        return {"error": "DB connection failed", "processed": 0, "inserted": 0, "updated": 0}

    query_insert = """
        INSERT INTO athena (
            account_name, account_id, query_id, query_name, domain,
            description, database_name, tables_used, execution_duration,
            execution_frequency, owner, region, last_updated
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP
        )
    """

    inserted = 0
    updated = 0
    processed = 0

    try:
        cursor = conn.cursor()

        # Obtener datos existentes
        cursor.execute("SELECT * FROM athena")
        columns = [desc[0].lower() for desc in cursor.description]
        existing_data = {(row[columns.index("query_id")], row[columns.index("account_id")]): dict(zip(columns, row)) for row in cursor.fetchall()}

        for query in athena_data:
            query_id = query["QueryId"]
            processed += 1

            insert_values = (
                query["AccountName"], query["AccountID"], query["QueryId"],
                query["QueryName"], query["Domain"], query["Description"],
                query["Database"], query["TablesUsed"], query["ExecutionDuration"],
                query["ExecutionFrequency"], query["Owner"], query["Region"]
            )

            if (query_id, query["AccountID"]) not in existing_data:
                cursor.execute(query_insert, insert_values)
                inserted += 1
            else:
                db_row = existing_data[(query_id, query["AccountID"])]
                updates = []
                values = []

                campos = {
                    "account_name": query["AccountName"],
                    "account_id": query["AccountID"],
                    "query_id": query["QueryId"],
                    "query_name": query["QueryName"],
                    "domain": query["Domain"],
                    "description": query["Description"],
                    "database_name": query["Database"],
                    "tables_used": query["TablesUsed"],
                    "execution_duration": query["ExecutionDuration"],
                    "execution_frequency": query["ExecutionFrequency"],
                    "owner": query["Owner"],
                    "region": query["Region"]
                }

                # Verificar si cambió el account_id o query_id (campos de identificación)
                if (str(db_row.get('account_id')) != str(query["AccountID"]) or 
                    str(db_row.get('query_id')) != str(query["QueryId"])):
                    # Si cambió la identificación, insertar como nuevo registro
                    cursor.execute(query_insert, insert_values)
                    inserted += 1
                    continue

                for col, new_val in campos.items():
                    # Saltar campos de identificación para actualizaciones
                    if col in ['account_id', 'query_id']:
                        continue
                    
                    old_val = db_row.get(col)
                    if str(old_val) != str(new_val):
                        updates.append(f"{col} = %s")
                        values.append(new_val)
                        changed_by = get_query_changed_by(query_id, datetime.now())
                        log_change('ATHENA', query_id, col, old_val, new_val, changed_by, query["AccountID"], query["Region"])

                updates.append("last_updated = CURRENT_TIMESTAMP")

                if updates:
                    update_query = f"UPDATE athena SET {', '.join(updates)} WHERE query_id = %s AND account_id = %s"
                    values.extend([query_id, query["AccountID"]])
                    cursor.execute(update_query, tuple(values))
                    updated += 1

        conn.commit()
        return {
            "processed": processed,
            "inserted": inserted,
            "updated": updated
        }

    except Exception as e:
        conn.rollback()
        pass
        return {"error": str(e), "processed": 0, "inserted": 0, "updated": 0}
    finally:
        conn.close()