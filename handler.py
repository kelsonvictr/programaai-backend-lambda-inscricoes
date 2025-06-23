import json
import boto3
import uuid
import os
import requests
from datetime import datetime, timedelta, timezone

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Inscricoes')

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

def salvar_inscricao(event, context):
    # Pré-voo (CORS)
    if event['httpMethod'] == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({'message': 'CORS OK'})
        }

    try:
        body = json.loads(event['body'])

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
            'dataInscricao': datetime.now(timezone(timedelta(hours=-3))).isoformat()
        }

        table.put_item(Item=item)

        try:
            enviar_para_telegram(item)
        except Exception as err:
            print("Erro ao enviar Telegram:", err)

        return {
            'statusCode': 201,
            'headers': cors_headers(),
            'body': json.dumps({'message': 'Inscrição realizada com sucesso!'})
        }

    except Exception as e:
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
🕒 Data: {inscricao['dataInscricao']}
"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        'chat_id': TELEGRAM_CHAT_ID,
        'text': mensagem
    })

def cors_headers():
    return {
        'Access-Control-Allow-Origin': 'https://programaai.dev/',
        'Access-Control-Allow-Headers': '*',
        'Access-Control-Allow-Methods': 'OPTIONS,POST',
        'Content-Type': 'application/json'
    }
