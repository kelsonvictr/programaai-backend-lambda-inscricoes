import json
import boto3
import uuid
from datetime import datetime

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Inscricoes')

def salvar_inscricao(event, context):
    body = json.loads(event['body'])

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

    table.put_item(Item=item)

    return {
        'statusCode': 201,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({'message': 'Inscrição realizada com sucesso!'})
    }
