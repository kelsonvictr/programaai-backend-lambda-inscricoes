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

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
ASAAS_API_KEY = os.environ.get('ASAAS_API_KEY')
ASAAS_ENDPOINT = "https://www.asaas.com/api/v3"

def salvar_inscricao(event, context):
    logger.info("Evento recebido: %s", json.dumps(event))

    if event.get('httpMethod') == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({'message': 'CORS OK'})
        }

    try:
        body_raw = event.get('body')
        body = json.loads(body_raw)

        if body.get("website"):
            logger.warning("Tentativa de bot detectada.")
            return {
                'statusCode': 400,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'Solicitação inválida.'})
            }

        agora = datetime.now(timezone(timedelta(hours=-3))).isoformat()
        ip = event.get('requestContext', {}).get('identity', {}).get('sourceIp', 'desconhecido')
        user_agent = event.get('headers', {}).get('User-Agent', 'desconhecido')

        inscricao_id = str(uuid.uuid4())

        item = {
            'id': inscricao_id,
            'curso': body['curso'],
            'nomeCompleto': body['nomeCompleto'],
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
            'userAgent': user_agent
        }

        # cria cobrança no Asaas
        cobranca = criar_cobranca_asaas(body['nomeCompleto'], body['email'], body['whatsapp'], 199.99)
        item['asaasPaymentId'] = cobranca.get('id', '')
        item['asaasPaymentLink'] = cobranca.get('invoiceUrl', '')

        table.put_item(Item=item)
        logger.info("Item salvo no DynamoDB e cobrança criada")

        try:
            enviar_para_telegram(item, cobranca.get('invoiceUrl', ''))
        except Exception as err:
            logger.error("Erro ao enviar para o Telegram: %s", err)

        return {
            'statusCode': 201,
            'headers': cors_headers(),
            'body': json.dumps({
                'message': 'Inscrição e cobrança criadas com sucesso!',
                'linkPagamento': cobranca.get('invoiceUrl', '')
            })
        }

    except Exception as e:
        logger.error("Erro inesperado: %s", e, exc_info=True)
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }


def criar_cobranca_asaas(nome, email, telefone, valor):
    logger.info(f"4p1: {ASAAS_API_KEY}")
    headers = {
        "Content-Type": "application/json",
        "access_token": ASAAS_API_KEY
    }

    payload = {
        "customer": criar_cliente_asaas(nome, email, telefone),
        "billingType": "UNDEFINED",  # cliente escolhe Pix ou Cartão
        "value": valor,
        "dueDate": datetime.now().strftime("%Y-%m-%d"),
        "description": "Inscrição no curso",
        "externalReference": str(uuid.uuid4())
    }

    response = requests.post(f"{ASAAS_ENDPOINT}/payments", headers=headers, json=payload)
    response.raise_for_status()
    return response.json()


def criar_cliente_asaas(nome, email, telefone):
    headers = {
        "Content-Type": "application/json",
        "access_token": ASAAS_API_KEY
    }

    payload = {
        "name": nome,
        "email": email,
        "phone": telefone
    }

    response = requests.post(f"{ASAAS_ENDPOINT}/customers", headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    return data.get("id")


def enviar_para_telegram(inscricao, link_pagamento):
    mensagem = f"""
📩 Nova inscrição recebida!

📚 Curso: {inscricao['curso']}
👤 Nome: {inscricao['nomeCompleto']}
📧 Email: {inscricao['email']}
📱 WhatsApp: {inscricao['whatsapp']}
⚧ Sexo: {inscricao['sexo']}
🎂 Nascimento: {inscricao['dataNascimento']}
🎓 Formação TI: {inscricao['formacaoTI']}
🏫 Onde estuda: {inscricao.get('ondeEstuda', '')}
📣 Como soube: {inscricao['comoSoube']}
👥 Amigo: {inscricao.get('nomeAmigo', '')}
🛡️ Aceitou os termos: Sim
🕒 Data: {inscricao['dataInscricao']}
💳 Link de pagamento: {link_pagamento}
🖥️ IP / Navegador: {inscricao['ip']} / {inscricao['userAgent']}
""".strip()

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(url, json={
        'chat_id': TELEGRAM_CHAT_ID,
        'text': mensagem
    })
    logger.info("Resposta do Telegram: %s", response.text)


def cors_headers():
    return {
        'Access-Control-Allow-Origin': 'https://programaai.dev',
        'Access-Control-Allow-Headers': '*',
        'Access-Control-Allow-Methods': 'OPTIONS,POST',
        'Content-Type': 'application/json'
    }
