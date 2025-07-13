import json
import boto3
import uuid
import os
import requests
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Inscricoes')
ses = boto3.client('ses')

ASAAS_API_KEY = os.environ.get('ASAAS_API_KEY')
ASAAS_ENDPOINT = "https://www.asaas.com/api/v3"
ADMIN_KEY = os.environ.get('ADMIN_KEY')
REMETENTE = 'Programa AI <no-reply@programaai.dev>'

def salvar_inscricao(event, context):
    logger.info("Evento recebido: %s", json.dumps(event))

    path = event.get("path", "")
    method = event.get("httpMethod", "")

    if path.startswith("/dev/admin"):
        api_key = event["headers"].get("x-api-key")
        if api_key != ADMIN_KEY:
            return resposta(403, {'error': 'Unauthorized'})

        if path == "/dev/admin/inscricoes" and method == "GET":
            return listar_inscricoes()

        if path.startswith("/dev/admin/inscricoes/") and method == "DELETE":
            inscricao_id = path.split("/")[-1]
            return remover_inscricao(inscricao_id)

        return resposta(404, {'error': 'Admin route not found'})

    # CORS
    if event.get('httpMethod') == 'OPTIONS':
        return resposta(200, {'message': 'CORS OK'})

    # fluxo normal de inscrição
    try:
        body_raw = event.get('body')
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

def listar_inscricoes():
    try:
        result = table.scan()
        return resposta(200, result.get("Items", []))
    except Exception as e:
        logger.error("Erro ao listar: %s", e, exc_info=True)
        return resposta(500, {'error': str(e)})

def remover_inscricao(inscricao_id):
    try:
        table.delete_item(Key={'id': inscricao_id})
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
