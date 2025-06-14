#!/usr/bin/env python3
import boto3, argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from botocore.exceptions import ClientError

from listadoDeRoles import ROLES
from config import Regions
from services import (
    get_ec2_instances, insert_or_update_ec2_data,
    get_rds_instances, insert_or_update_rds_data,
    get_redshift_clusters, insert_or_update_redshift_data,
    get_vpc_details, insert_or_update_vpc_data,
    get_subnets_details, insert_or_update_subnet_data,
    get_ec2_cloudtrail_events, insert_or_update_cloudtrail_events,
    get_rds_cloudtrail_events, get_vpc_cloudtrail_events
)

def assume_role(role_arn):
    """Asume un rol IAM y devuelve credenciales temporales."""
    try:
        creds = boto3.client("sts").assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"EC2Session-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            DurationSeconds=900
        )["Credentials"]
        return {k: creds[k] for k in ["AccessKeyId", "SecretAccessKey", "SessionToken"]}
    except ClientError as e:
        print(f"Error al asumir el rol {role_arn}: {str(e)}")
        return {"error": str(e)}

def process_account_region(account_id, role_name, account_name, region, services):
    """Procesa una combinación de cuenta/región para los servicios solicitados."""
    start = datetime.now()
    creds = assume_role(f"arn:aws:iam::{account_id}:role/{role_name}")
    if "error" in creds:
        print(f"[{account_id}:{region}] Error al asumir rol: {creds['error']}")
        return {"account_id": account_id, "region": region, "error": creds["error"]}

    service_funcs = {
        "ec2": lambda: get_ec2_instances(region, creds, account_id, account_name),
        "rds": lambda: get_rds_instances(region, creds, account_id, account_name),
        "redshift": lambda: get_redshift_clusters(region, creds, account_id, account_name),
        "vpc": lambda: get_vpc_details(region, creds, account_id, account_name),
        "subnets": lambda: get_subnets_details(region, creds, account_id, account_name),
        "ec2_cloudtrail": lambda: get_ec2_cloudtrail_events(region, creds).get("events", []),
        "rds_cloudtrail": lambda: get_rds_cloudtrail_events(region, creds).get("events", []),
        "vpc_cloudtrail": lambda: get_vpc_cloudtrail_events(region, creds).get("events", [])
    }

    result = {"account_id": account_id, "region": region, "credentials": creds}
    for service in services:
        if (datetime.now() - start).total_seconds() > 300:  # 5 min timeout
            print(f"[{account_id}:{region}] Tiempo límite excedido")
            break
        try:
            key = f"{service}_data" if service != "cloudtrail_events" else service
            result[key] = service_funcs.get(service, lambda: [])()
        except Exception as e:
            print(f"[{account_id}:{region}] Error en {service}: {str(e)}")
    
    print(f"[{account_id}:{region}] Completado en {(datetime.now() - start).total_seconds():.2f}s")
    return result

def main(services):
    """Función principal que coordina la recolección de datos."""
    start = datetime.now()
    print(f"=== Iniciando proceso: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"Servicios: {', '.join(services)}")

    errors, collected_data, messages = {}, {s: [] for s in services}, []
    max_workers = min(10, len(ROLES) * len(Regions))
    total_jobs = len(ROLES) * len(Regions)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(process_account_region, r["id"], r["role"], 
                          r["account"], reg, services)
            for r in ROLES for reg in Regions
        ]
        
        for i, future in enumerate(as_completed(futures), 1):
            res = future.result()
            print(f"[Progreso] {i}/{total_jobs} ({i/total_jobs*100:.1f}%)")
            
            if "error" in res:
                errors.setdefault(res["account_id"], []).append(f"{res['region']}: {res['error']}")
                continue
            
            for s in services:
                key = f"{s}_data" if s != "cloudtrail_events" else s
                collected_data[s].extend([{
                    "data": d, "credentials": res["credentials"],
                    "region": res["region"], "account_id": res["account_id"]
                } for d in res.get(key, [])])

    insert_funcs = {
        "ec2": insert_or_update_ec2_data,
        "rds": insert_or_update_rds_data,
        "redshift": insert_or_update_redshift_data,
        "vpc": insert_or_update_vpc_data,
        "subnets": insert_or_update_subnet_data,
        "ec2_cloudtrail": insert_or_update_cloudtrail_events,
        "rds_cloudtrail": insert_or_update_cloudtrail_events,
        "vpc_cloudtrail": insert_or_update_cloudtrail_events
    }

    print("\n=== Insertando datos en la base de datos ===")
    for s in services:
        entries = collected_data.get(s, [])
        if not entries:
            messages.append(f"{s.upper()}: No hay datos para insertar")
            continue
        
        grouped = {}
        for e in entries:
            key = (e["region"], tuple(sorted(e["credentials"].items())))
            grouped.setdefault(key, []).append(e["data"])
        
        for (reg, _), data in grouped.items():
            res = insert_funcs[s](data)
            messages.append(
                f"{s.upper()} ({reg}): {len(data)} items "
                f"({res.get('inserted', 0)} insertados, {res.get('updated', 0)} actualizados)"
            )

    print("\n=== Resultados ===")
    print("\n".join(messages))
    if errors:
        print(f"\nErrores en {len(errors)} cuentas:")
        for acc, errs in errors.items():
            print(f"- Cuenta {acc}: {len(errs)} errores")
    
    print(f"\n=== Proceso completado en {(datetime.now() - start).total_seconds():.2f} segundos ===")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Recolecta información de recursos AWS')
    parser.add_argument('--services', nargs='+', default=["ec2", "ec2_cloudtrail"],
                      choices=["ec2", "rds", "redshift", "vpc", "subnets", "ec2_cloudtrail", "rds_cloudtrail", "vpc_cloudtrail"],
                      help='Servicios a consultar')
    main(parser.parse_args().services)