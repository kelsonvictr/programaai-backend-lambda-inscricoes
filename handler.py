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
        logger.info("Body recebido bruto: %s", body_raw)
        body = json.loads(body_raw)

        if body.get("website"):
            logger.warning("Tentativa de bot detectada. Campo 'website' foi preenchido.")
            return {
                'statusCode': 400,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'Solicitação inválida.'})
            }

        agora = datetime.now(timezone(timedelta(hours=-3))).isoformat()
        ip = event.get('requestContext', {}).get('identity', {}).get('sourceIp', 'desconhecido')
        user_agent = event.get('headers', {}).get('User-Agent', 'desconhecido')

        item = {
            'id': str(uuid.uuid4()),
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

        logger.info("Registro da inscrição com aceite de termos: %s", json.dumps({
            "evento": "inscricao_realizada",
            "timestamp": agora,
            "ip": ip,
            "userAgent": user_agent,
            "nome": body['nomeCompleto'],
            "email": body['email'],
            "curso": body['curso'],
            "aceitouTermos": True
        }))

        table.put_item(Item=item)
        logger.info("Item salvo com sucesso no DynamoDB")

        try:
            enviar_para_telegram(item)
            logger.info("Mensagem enviada para o Telegram com sucesso")
        except Exception as err:
            logger.error("Erro ao enviar mensagem para o Telegram: %s", err)

        return {
            'statusCode': 201,
            'headers': cors_headers(),
            'body': json.dumps({'message': 'Inscrição realizada com sucesso!'})
        }

    except Exception as e:
        logger.error("Erro inesperado: %s", e, exc_info=True)
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }

def enviar_para_telegram(inscricao):
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
