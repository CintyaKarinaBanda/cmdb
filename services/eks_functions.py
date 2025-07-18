from botocore.exceptions import ClientError
from datetime import datetime
import time
from services.utils import create_aws_client, get_db_connection, log_change

def get_local_time():
    return 'NOW()'

FIELD_EVENT_MAP = {
    "clustername": ["CreateCluster", "UpdateClusterConfig"],
    "status": ["CreateCluster", "DeleteCluster", "UpdateClusterConfig"],
    "kubernetesversion": ["UpdateClusterVersion"],
    "supportperiod": ["UpdateClusterConfig"],
    "addons": ["CreateAddon", "DeleteAddon", "UpdateAddon"],
    "tags": ["TagResource", "UntagResource"]
}

def normalize_list_comparison(old_val, new_val):
    """Normaliza listas para comparación, ignorando orden"""
    if isinstance(new_val, list) and isinstance(old_val, (list, str)):
        old_list = old_val if isinstance(old_val, list) else str(old_val).split(',') if old_val else []
        return sorted([str(x).strip() for x in old_list]) == sorted([str(x).strip() for x in new_val])
    return str(old_val) == str(new_val)

def get_cluster_changed_by(cluster_name, field_name):
    conn = get_db_connection()
    if not conn:
        return "unknown"
    try:
        with conn.cursor() as cursor:
            events = FIELD_EVENT_MAP.get(field_name, [])
            if events:
                placeholders = ','.join(['%s'] * len(events))
                cursor.execute(f"SELECT user_name FROM cloudtrail_events WHERE resource_name = %s AND resource_type = 'EKS' AND event_name IN ({placeholders}) ORDER BY event_time DESC LIMIT 1", (cluster_name, *events))
            else:
                cursor.execute("SELECT user_name FROM cloudtrail_events WHERE resource_name = %s AND resource_type = 'EKS' ORDER BY event_time DESC LIMIT 1", (cluster_name,))
            return cursor.fetchone()[0] if cursor.fetchone() else "unknown"
    except:
        return "unknown"
    finally:
        conn.close()

def extract_eks_data(cluster, eks_client, account_name, account_id, region):
    try:
        addons = eks_client.list_addons(clusterName=cluster["name"]).get('addons', [])
    except:
        addons = []
    
    version = cluster.get("version", "")
    support_type = cluster.get("supportType", "STANDARD")
    dates = {"1.31": "Nov 25, 2025", "1.30": "Jul 25, 2025", "1.29": "Mar 25, 2025"}
    support_msg = f"{support_type.title()} - Ends {dates.get(version, 'N/A')}" if version else "Standard"
    
    return {
        "AccountName": account_name,
        "AccountID": account_id,
        "ClusterID": cluster.get("arn", cluster["name"]).split("/")[-1],
        "ClusterName": cluster["name"],
        "Status": cluster.get("status", "N/A"),
        "KubernetesVersion": version or "N/A",
        "Provider": "AWS",
        "ClusterSecurityGroup": cluster.get("resourcesVpcConfig", {}).get("clusterSecurityGroupId", "N/A"),
        "SupportPeriod": support_msg,
        "Addons": addons,
        "Tags": cluster.get("tags", {})
    }

def get_eks_clusters(region, credentials, account_id, account_name):
    eks_client = create_aws_client("eks", region, credentials)
    if not eks_client:
        return []
    try:
        clusters_info = []
        for page in eks_client.get_paginator('list_clusters').paginate():
            for cluster_name in page.get("clusters", []):
                try:
                    cluster = eks_client.describe_cluster(name=cluster_name).get("cluster", {})
                    clusters_info.append(extract_eks_data(cluster, eks_client, account_name, account_id, region))
                except:
                    continue
        return clusters_info
    except:
        return []

def insert_or_update_eks_data(eks_data):
    if not eks_data:
        return {"processed": 0, "inserted": 0, "updated": 0}
    conn = get_db_connection()
    if not conn:
        return {"error": "DB connection failed", "processed": 0, "inserted": 0, "updated": 0}
    
    inserted = updated = processed = 0
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM eks")
        columns = [desc[0].lower() for desc in cursor.description]
        existing = {(row[columns.index("clustername")], row[columns.index("accountid")]): dict(zip(columns, row)) for row in cursor.fetchall()}
        
        for eks in eks_data:
            processed += 1
            cluster_name = eks["ClusterName"]
            values = (eks["AccountName"], eks["AccountID"], eks["ClusterID"], eks["ClusterName"], eks["Status"], eks["KubernetesVersion"], eks["Provider"], eks["ClusterSecurityGroup"], eks["SupportPeriod"], eks["Addons"], eks["Tags"])
            
            if (cluster_name, eks["AccountID"]) not in existing:
                cursor.execute("INSERT INTO eks (AccountName, AccountID, ClusterID, ClusterName, Status, KubernetesVersion, Provider, ClusterSecurityGroup, SupportPeriod, Addons, Tags, last_updated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())", values)
                inserted += 1
            else:
                db_row = existing[(cluster_name, eks["AccountID"])]
                updates = []
                vals = []
                campos = {"accountname": eks["AccountName"], "accountid": eks["AccountID"], "clusterid": eks["ClusterID"], "clustername": eks["ClusterName"], "status": eks["Status"], "kubernetesversion": eks["KubernetesVersion"], "provider": eks["Provider"], "clustersecuritygroup": eks["ClusterSecurityGroup"], "supportperiod": eks["SupportPeriod"], "addons": eks["Addons"], "tags": eks["Tags"]}
                
                # Verificar si cambió el account_id o cluster_name (campos de identificación)
                if (str(db_row.get('accountid')) != str(eks["AccountID"]) or 
                    str(db_row.get('clustername')) != str(eks["ClusterName"])):
                    # Si cambió la identificación, insertar como nuevo registro
                    cursor.execute("INSERT INTO eks (AccountName, AccountID, ClusterID, ClusterName, Status, KubernetesVersion, Provider, ClusterSecurityGroup, SupportPeriod, Addons, Tags, last_updated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())", values)
                    inserted += 1
                    continue
                
                for col, new_val in campos.items():
                    # Saltar campos de identificación para actualizaciones
                    if col in ['accountid', 'clustername']:
                        continue
                    
                    old_val = db_row.get(col)
                    if not normalize_list_comparison(old_val, new_val):
                        updates.append(f"{col} = %s")
                        vals.append(new_val)
                        log_change('EKS', cluster_name, col, old_val, new_val, get_cluster_changed_by(cluster_name, col), eks["AccountID"], 'N/A')
                
                if updates:
                    cursor.execute(f"UPDATE eks SET {', '.join(updates)}, last_updated = NOW() WHERE clustername = %s", vals + [cluster_name])
                    updated += 1
        
        conn.commit()
        return {"processed": processed, "inserted": inserted, "updated": updated}
    except Exception as e:
        conn.rollback()
        return {"error": str(e), "processed": 0, "inserted": 0, "updated": 0}
    finally:
        conn.close()