import json
import boto3
import uuid
import os
from datetime import datetime

# Inicializa DynamoDB e SES
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Inscricoes')

ses = boto3.client('ses', region_name='us-east-1')


def salvar_inscricao(event, context):
    try:
        body = json.loads(event['body'])

        # Monta o item a ser salvo no banco
        item = {
            'id': str(uuid.uuid4()),
            'nomeCompleto': body['nomeCompleto'],
            'email': body['email'],
            'whatsapp': body['whatsapp'],
            'sexo': body['sexo'],
            'dataNascimento': body['dataNascimento'],
            'formacaoTI': body['formacaoTI'],
            'ondeEstuda': body.get('ondeEstuda', ''),
            'comoSoube': body['comoSoube'],
            'nomeAmigo': body.get('nomeAmigo', ''),
            'dataInscricao': datetime.utcnow().isoformat()
        }

        # Salva no DynamoDB
        table.put_item(Item=item)

        # Envia e-mail de notificação
        enviar_email_para_admin(item)

        # Retorna resposta de sucesso
        return {
            'statusCode': 201,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': "http://programaai-site.s3-website-us-east-1.amazonaws.com",
                'Access-Control-Allow-Headers': '*',
                'Access-Control-Allow-Methods': 'OPTIONS,POST'
            },
            'body': json.dumps({'message': 'Inscrição realizada com sucesso!'})
        }


    except Exception as e:
        # Em caso de erro, retorna mensagem e status 500
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': 'https://programaai-site.s3-website-us-east-1.amazonaws.com',
                'Access-Control-Allow-Headers': '*',
                'Access-Control-Allow-Methods': 'OPTIONS,POST'
            },
            'body': json.dumps({'error': str(e)})
        }


def enviar_email_para_admin(inscricao):
    # Monta o corpo do e-mail
    mensagem = f"""
Nova inscrição recebida:

Nome: {inscricao['nomeCompleto']}
Email: {inscricao['email']}
WhatsApp: {inscricao['whatsapp']}
Sexo: {inscricao['sexo']}
Nascimento: {inscricao['dataNascimento']}
Formação TI: {inscricao['formacaoTI']}
Onde Estuda: {inscricao.get('ondeEstuda', '')}
Como soube: {inscricao['comoSoube']}
Amigo: {inscricao.get('nomeAmigo', '')}
Data de Inscrição: {inscricao['dataInscricao']}
"""

    # Envia o e-mail via SES
    ses.send_email(
        Source=os.environ['EMAIL_ORIGEM'],
        Destination={'ToAddresses': [os.environ['EMAIL_DESTINO']]},
        Message={
            'Subject': {'Data': 'Nova Inscrição Recebida'},
            'Body': {'Text': {'Data': mensagem}}
        }
    )
