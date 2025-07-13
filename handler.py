import json
import boto3
import uuid
import os
import requests
import logging
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import auth, credentials

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Inscricoes')
ses = boto3.client('ses')
s3 = boto3.client('s3')

ASAAS_API_KEY = os.environ.get('ASAAS')
ASAAS_ENDPOINT = "https://www.asaas.com/api/v3"
REMETENTE = 'Programa AI <no-reply@programaai.dev>'

FIREBASE_BUCKET = os.environ.get('FIREBASE_BUCKET')
FIREBASE_KEY_PATH = os.environ.get('FIREBASE_KEY_PATH')

def init_firebase():
    if not firebase_admin._apps:
        logger.info(f"Carregando chave Firebase do bucket {FIREBASE_BUCKET}/{FIREBASE_KEY_PATH}")
        obj = s3.get_object(Bucket=FIREBASE_BUCKET, Key=FIREBASE_KEY_PATH)
        json_key = json.load(obj['Body'])
        cred = credentials.Certificate(json_key)
        firebase_admin.initialize_app(cred)

init_firebase()

def salvar_inscricao(event, context):
    logger.info("Evento recebido: %s", json.dumps(event))

    path = event.get("path", "")
    method = event.get("httpMethod", "")
    logger.info(f"Path recebido: {path} | Method: {method}")

    if "/galaxy" in path:
        try:
            auth_header = event["headers"].get("Authorization", "")
            uid, email = validar_jwt(auth_header)
            logger.info(f"Usuário autenticado: {email} ({uid})")
        except Exception as e:
            logger.error(f"Falha na autenticação: {e}")
            return resposta(401, {'error': 'Unauthorized'})

        if path.endswith("/galaxy/inscricoes") and method == "GET":
            return listar_inscricoes()

        if path.startswith("/galaxy/inscricoes/") and method == "DELETE":
            inscricao_id = path.split("/")[-1]
            return remover_inscricao(inscricao_id)

        return resposta(404, {'error': 'Admin route not found'})

    if method == 'OPTIONS':
        return resposta(200, {'message': 'CORS OK'})

    if path.endswith("/inscricao") and method == "POST":
        try:
            body_raw = event.get('body')
            if not body_raw:
                return resposta(400, {'error': 'Body is required'})

            body = json.loads(body_raw)

            if body.get("website"):
                logger.warning("Tentativa de bot detectada.")
                return resposta(400, {'error': 'Solicitação inválida.'})

            agora = datetime.now(timezone(timedelta(hours=-3))).isoformat()
            ip = event.get('requestContext', {}).get('identity', {}).get('sourceIp', 'desconhecido')
            user_agent = event.get('headers', {}).get('User-Agent', 'desconhecido')

            inscricao_id = str(uuid.uuid4())
            external_ref = str(uuid.uuid4())

            valor_curso = float(body['valor'])
            payment_method = body.get('paymentMethod', 'PIX').upper()
            nome_curso = body['curso']
            nome_aluno = body['nomeCompleto']
            cpf_aluno = body.get('cpf', '')
            rg_aluno = body.get('rg', '')

            payment_link = criar_paymentlink_asaas(
                nome_curso, nome_aluno, cpf_aluno, valor_curso, payment_method, external_ref
            )

            item = {
                'id': inscricao_id,
                'curso': nome_curso,
                'nomeCompleto': nome_aluno,
                'cpf': cpf_aluno,
                'rg': rg_aluno,
                'email': body['email'],
                'whatsapp': body['whatsapp'],
                'sexo': body['sexo'],
                'dataNascimento': body['dataNascimento'],
                'formacaoTI': body['formacaoTI'],
                'ondeEstuda': body.get('ondeEstuda', ''),
                'comoSoube': body['comoSoube'],
                'nomeAmigo': body.get('nomeAmigo', ''),
                'dataInscricao': agora,
                'aceitouTermos': True,
                'ip': ip,
                'userAgent': user_agent,
                'asaasPaymentLinkId': payment_link.get('id', ''),
                'asaasPaymentLinkUrl': payment_link.get('url', ''),
                'asaasExternalReference': external_ref,
                'paymentMethod': payment_method
            }

            table.put_item(Item=item)

            logger.info("Item salvo no DynamoDB e paymentLink criado")

            return resposta(201, {
                'message': 'Inscrição e link de pagamento criados com sucesso!',
                'linkPagamento': payment_link.get('url', '')
            })

        except Exception as e:
            logger.error("Erro inesperado: %s", e, exc_info=True)
            return resposta(500, {'error': str(e)})

    return resposta(404, {'error': 'Route not found'})

def listar_inscricoes():
    try:
        result = table.scan()
        logger.info(f"Total inscrições encontradas: {len(result.get('Items', []))}")
        return resposta(200, result.get("Items", []))
    except Exception as e:
        logger.error("Erro ao listar: %s", e, exc_info=True)
        return resposta(500, {'error': str(e)})

def remover_inscricao(inscricao_id):
    try:
        table.delete_item(Key={'id': inscricao_id})
        logger.info(f"Inscrição {inscricao_id} removida")
        return resposta(200, {'message': 'Inscrição removida com sucesso'})
    except Exception as e:
        logger.error("Erro ao remover: %s", e, exc_info=True)
        return resposta(500, {'error': str(e)})

def criar_paymentlink_asaas(curso, aluno, cpf, valor, metodo, external_ref):
    headers = {
        "Content-Type": "application/json",
        "access_token": ASAAS_API_KEY
    }

    nome = f"Inscrição: {curso}"
    descricao = f"Inscrição: {curso}. Aluno(a): {aluno} - CPF: {cpf}"

    if metodo == 'PIX':
        payload = {
            "name": nome,
            "billingType": "PIX",
            "chargeType": "DETACHED",
            "value": valor,
            "description": descricao,
            "dueDateLimitDays": 2,
            "externalReference": external_ref,
            "notificationEnabled": True
        }
    elif metodo == 'CARTAO':
        valor_com_taxa = round(valor * 1.08, 2)
        payload = {
            "name": nome,
            "billingType": "CREDIT_CARD",
            "chargeType": "INSTALLMENT",
            "value": valor_com_taxa,
            "description": descricao,
            "dueDateLimitDays": 7,
            "maxInstallmentCount": 12,
            "externalReference": external_ref,
            "notificationEnabled": True
        }
    else:
        raise ValueError(f"Método de pagamento inválido: {metodo}")

    response = requests.post(f"{ASAAS_ENDPOINT}/paymentLinks", headers=headers, json=payload)
    response.raise_for_status()
    return response.json()

def validar_jwt(authorization_header):
    if not authorization_header:
        raise Exception("Authorization header missing")
    parts = authorization_header.split()
    if len(parts) != 2 or parts[0] != "Bearer":
        raise Exception("Invalid authorization header")

    token = parts[1]
    decoded_token = auth.verify_id_token(token)
    uid = decoded_token['uid']
    email = decoded_token.get('email')
    return uid, email

def resposta(status, body):
    return {
        'statusCode': status,
        'headers': cors_headers(),
        'body': json.dumps(body)
    }

def cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': '*',
        'Access-Control-Allow-Methods': 'OPTIONS,GET,POST,DELETE',
        'Content-Type': 'application/json'
    }
