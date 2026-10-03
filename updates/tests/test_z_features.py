"""Feature integration tests: authorization boundaries and verified payment transitions."""
import hashlib
import hmac
import io
import time
from datetime import date, time as clocktime
import pytest
from bs4 import BeautifulSoup
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from tests.test_flow import token
from app.main import app
from app.database import SessionLocal, engine
from app.models import Admin, Event, EventAdmin, Member, Registration, SiteImage
from app.security import password_hash
from app.media import catalog
from app import payments

PASSWORD = 'SecureFeatureTestPassword123!'

@pytest.fixture(autouse=True)
def reset_test_rate_limits():
    from app.main import MEMBER_AUTH_ATTEMPTS, LOGIN_FAILURES, SIGNUPS
    MEMBER_AUTH_ATTEMPTS.clear(); LOGIN_FAILURES.clear(); SIGNUPS.clear()


def login(client, email='admin@example.com'):
    page = client.get('/admin/entrar')
    response = client.post('/admin/entrar',data={'csrf':token(page),'email':email,'password':'StrongTestPassword123!' if email=='admin@example.com' else PASSWORD},follow_redirects=False)
    assert response.status_code==303


def create_event(mode, slug):
    with SessionLocal() as db:
        event=Event(slug=slug,title='Atividade de teste '+slug,description='',event_date=date(2026,10,20),starts_at=clocktime(15),ends_at=clocktime(17),location_name='Parque',address='Curitiba',capacity=8,price_cents=1500 if mode=='paid' else 0,mode=mode,followup_url='https://example.com/acompanhamento',extra_links='[{"label":"Grupo","url":"https://example.com/grupo"}]')
        db.add(event);db.commit();return event.id


def member_client(email):
    client=TestClient(app)
    page=client.get('/conta/cadastrar')
    assert client.post('/conta/cadastrar',data={'csrf':token(page),'full_name':'Pessoa Teste Conta','email':email,'password':PASSWORD,'consent':'yes'},follow_redirects=False).status_code==303
    return client


def test_a_scope_assignment_all_legacy_routes_and_multiple_events():
    ids=[create_event('free','scope-one'),create_event('free','scope-two'),create_event('free','scope-blocked')]
    scoped_member=member_client('scoped@example.com')
    with SessionLocal() as db:
        outsider=Registration(event_id=ids[2],full_name='Pessoa fora do escopo',payment_status='paid')
        db.add(outsider);db.commit();rid=outsider.id
    with TestClient(app) as master:
        login(master)
        page=master.get('/admin/responsaveis');assert page.status_code==200
        assert master.post('/admin/responsaveis',data={'csrf':token(page),'email':'scoped@example.com','event_ids':[str(ids[0]),str(ids[1])]},follow_redirects=False).status_code==303
    with TestClient(app) as scoped:
        login(scoped,'scoped@example.com')
        dashboard=scoped.get('/admin');assert 'scope-one' in dashboard.text and 'scope-two' in dashboard.text and 'scope-blocked' not in dashboard.text
        for eid in ids[:2]:
            assert scoped.get(f'/admin/eventos/{eid}').status_code==200
            assert scoped.get(f'/admin/eventos/{eid}/editar').status_code==200
        for suffix in ['', '/editar','/mensagem','/exportar.csv','/pagamentos','/pagamentos.png']:
            assert scoped.get(f'/admin/eventos/{ids[2]}'+suffix).status_code==403
        for path in ['/admin/fotos','/admin/responsaveis','/admin/pix','/admin/eventos/novo',f'/admin/inscricoes/{rid}/editar']:
            assert scoped.get(path).status_code==403
        csrf=token(dashboard)
        for path,data in [(f'/admin/inscricoes/{rid}/atualizar',{'payment_status':'paid'}),(f'/admin/inscricoes/{rid}/cancelar',{}),(f'/admin/inscricoes/{rid}/anonimizar',{}),(f'/admin/inscricoes/{rid}/editar',{'full_name':'Alteração proibida'}),(f'/admin/eventos/{ids[2]}/inscricoes/manual',{'full_name':'Pessoa Proibida'})]:
            assert scoped.post(path,data={'csrf':csrf,**data}).status_code==403
        # A granted event remains editable; invalid links never persist.
        eid=ids[0]
        form={'csrf':csrf,'title':'Atividade editada','event_date':'2026-10-20','starts_at':'15:00','ends_at':'17:00','location_name':'Parque','address':'Curitiba','capacity':'8','price':'19,00','mode':'free','is_published':'yes','followup_url':'https://example.com/seguir','extra_links':'Grupo | https://example.com/grupo','checkin_enabled':'yes'}
        assert scoped.post(f'/admin/eventos/{eid}/editar',data=form,follow_redirects=False).status_code==303
        with SessionLocal() as db:
            ev=db.get(Event,eid);assert ev.price_cents==0 and ev.followup_url.endswith('/seguir')
        assert scoped.post(f'/admin/eventos/{eid}/editar',data={**form,'extra_links':'Risco | javascript:alert(1)'}).status_code==400
        with SessionLocal() as db:
            assert 'javascript' not in db.get(Event,eid).extra_links
    scoped_member.close()


def test_b_photos_persistence_crop_original_and_upload_rejection():
    item=next(item for item in catalog().values() if 'hero-home' in item['canonical'])
    key=item['key']
    with TestClient(app) as master:
        login(master)
        page=master.get('/admin/fotos');assert page.status_code==200 and 'Fotos do site' in page.text
        csrf=token(page)
        # Edit the existing photo without requiring a replacement.
        assert master.post('/admin/fotos/'+key,data={'csrf':csrf,'alt':'Capa editada','ratio':'1:1'} ,follow_redirects=False).status_code==303
        with SessionLocal() as db:
            image=db.get(SiteImage,key);assert image.alt=='Capa editada'
            decoded=Image.open(io.BytesIO(image.data));assert decoded.width==decoded.height
        assert '/media/site/'+key in master.get('/').text
        raw=io.BytesIO();Image.new('RGB',(640,480),'green').save(raw,format='PNG')
        assert master.post('/admin/fotos/'+key,data={'csrf':csrf,'alt':'Foto de teste','rotation':'90','ratio':'16:9'},files={'file':('test.png',raw.getvalue(),'image/png')},follow_redirects=False).status_code==303
        assert master.post('/admin/fotos/'+key,data={'csrf':csrf},files={'file':('script.svg',b'<svg><script>bad</script></svg>','image/svg+xml')}).status_code==422
        first=master.get('/media/site/'+key);assert first.status_code==200 and first.headers['content-type']=='image/webp'
    engine.dispose()  # Persistence across fresh connections and app lifespan.
    with TestClient(app) as fresh:
        assert fresh.get('/media/site/'+key).content==first.content
        login(fresh)
        assert fresh.post('/admin/fotos/'+key+'/restaurar',data={'csrf':token(fresh.get('/admin/fotos'))},follow_redirects=False).status_code==303
        assert fresh.get('/media/site/'+key).status_code==404


def test_c_free_auto_account_checkin_consent_refusal_and_revocation():
    eid=create_event('free','free-flow')
    client=member_client('free-flow@example.com')
    page=client.get('/evento/free-flow')
    assert 'name="full_name"' not in page.text and 'name="phone"' not in page.text
    joined=client.post('/evento/free-flow/participar',data={'csrf':token(page)},follow_redirects=False)
    url=joined.headers['location'];page=client.get(url);assert 'Participação confirmada!' in page.text
    again=client.post('/evento/free-flow/participar',data={'csrf':token(page)},follow_redirects=False);assert again.headers['location']==url
    with SessionLocal() as db:
        registration=db.scalar(select(Registration).where(Registration.event_id==eid));assert registration.full_name=='Pessoa Teste Conta' and registration.email=='free-flow@example.com' and registration.payment_status=='exempt'
    # Freely given: refusal is allowed, and must never be recorded as consent.
    assert client.post(url+'/check-in',data={'csrf':token(page)},follow_redirects=False).status_code==303
    with SessionLocal() as db:
        registration=db.scalar(select(Registration).where(Registration.event_id==eid));assert registration.attended and registration.image_consent_at is None
    other=member_client('free-second@example.com');page=other.get('/evento/free-flow')
    response=other.post('/evento/free-flow/participar',data={'csrf':token(page)},follow_redirects=False);other_url=response.headers['location'];page=other.get(other_url)
    assert client.get(other_url).status_code==404
    assert other.post(other_url+'/check-in',data={'csrf':token(page),'image_consent':'yes'},follow_redirects=False).status_code==303
    assert other.post(other_url+'/revogar-imagem',data={'csrf':token(other.get(other_url))},follow_redirects=False).status_code==303
    with SessionLocal() as db:
        reg=db.get(Registration,int(other_url.split('/')[-1]));assert reg.image_consent_at and reg.image_revoked_at and 'Versão 1' in reg.image_consent_text
    assert client.get('/conta').text.count('free-flow')>=1
    client.close();other.close()


def test_d_paid_provider_pending_verified_completion_webhook_and_export(monkeypatch):
    eid=create_event('paid','paid-flow')
    monkeypatch.setenv('MP_ACCESS_TOKEN','synthetic-token-only');monkeypatch.setenv('MP_WEBHOOK_SECRET','synthetic-webhook-secret')
    order_id='ORD01SYNTHETICORDER12345'
    status={'paid':False}
    def fake_request(method,path,**kwargs):
        with SessionLocal() as db:
            reg=db.scalar(select(Registration).where(Registration.event_id==eid))
            reference=reg.payment_reference
        if method=='POST':
            assert kwargs['json']['payer']=={'email':'paid-flow@example.com'}
            assert kwargs['json']['total_amount']=='15.00'
            assert kwargs['headers']['X-Idempotency-Key']==reference
        return {'id':order_id,'external_reference':reference,'total_amount':'15.00','currency_id':'BRL','country_code':'BRA','status':'processed' if status['paid'] else 'action_required','status_detail':'accredited' if status['paid'] else 'waiting_transfer','transactions':{'payments':[{'amount':'15.00','paid_amount':'15.00' if status['paid'] else '0','status':'processed' if status['paid'] else 'action_required','status_detail':'accredited' if status['paid'] else 'waiting_transfer','payment_method':{'id':'pix','qr_code':'000201-test-payload'}}]}}
    monkeypatch.setattr(payments,'provider_request',fake_request)
    client=member_client('paid-flow@example.com');page=client.get('/evento/paid-flow')
    assert 'name="full_name"' not in page.text
    result=client.post('/evento/paid-flow/participar',data={'csrf':token(page)},follow_redirects=False);assert result.status_code==303
    url=result.headers['location'];page=client.get(url)
    assert 'Pix Copia e Cola' in page.text and 'Pagamento concluído!' not in page.text
    assert client.get(url+'/qr.png').headers['content-type']=='image/png'
    assert client.post(url+'/check-in',data={'csrf':token(page),'image_consent':'yes'}).status_code==409
    client.post(url+'/verificar',data={'csrf':token(page)})
    assert 'Pagamento concluído!' not in client.get(url).text
    assert client.get(url+'/status').json()['paid'] is False
    with SessionLocal() as db:
        reg=db.get(Registration,int(url.split('/')[-1]))
        invalid=fake_request('GET','');invalid['total_amount']='0.01'
        with pytest.raises(HTTPException):payments.apply_order(reg,invalid)
    # Unsigned requests cannot mark any transaction paid.
    assert client.post('/webhooks/pix?data.id='+order_id,json={'data':{'id':order_id}}).status_code==401
    status['paid']=True
    stamp=str(int(time.time()*1000));request_id='synthetic-request'
    signature=hmac.new(b'synthetic-webhook-secret',f'id:{order_id.lower()};request-id:{request_id};ts:{stamp};'.encode(),hashlib.sha256).hexdigest()
    response=client.post('/webhooks/pix?data.id='+order_id,json={'data':{'id':order_id}},headers={'x-signature':f'ts={stamp},v1={signature}','x-request-id':request_id});assert response.status_code==200
    assert 'Pagamento concluído!' in client.get(url).text
    with SessionLocal() as db:
        reg=db.get(Registration,int(url.split('/')[-1]));assert reg.payment_status=='paid' and reg.paid_at and reg.payment_source=='mercado_pago'
        paid_at=reg.paid_at
    assert client.post('/webhooks/pix?data.id='+order_id,json={'data':{'id':order_id}},headers={'x-signature':f'ts={stamp},v1={signature}','x-request-id':request_id}).status_code==200
    with SessionLocal() as db:assert db.get(Registration,int(url.split('/')[-1])).paid_at==paid_at
    with TestClient(app) as master:
        login(master)
        listing=master.get(f'/admin/eventos/{eid}/pagamentos');assert 'Pix automático' in listing.text and 'Pessoa Teste Conta' in listing.text
        assert master.post(f'/admin/inscricoes/{url.split("/")[-1]}/atualizar',data={'csrf':token(listing),'payment_status':'pending'}).status_code==409
        png=master.get(f'/admin/eventos/{eid}/pagamentos.png?theme=claro&heading=TRIBO');assert png.status_code==200 and Image.open(io.BytesIO(png.content)).width==1200
    engine.dispose()
    assert 'Pagamento concluído!' in client.get(url).text
    client.close()


def test_e_pix_payload_crc_phone_and_missing_provider(monkeypatch):
    monkeypatch.delenv('MP_ACCESS_TOKEN',raising=False);monkeypatch.delenv('MP_WEBHOOK_SECRET',raising=False)
    monkeypatch.setenv('PIX_KEY','41 99999-1111')
    event=Event(price_cents=2000,pix_key='')
    payload=payments.pix_payload(event,'ETHOS1')
    assert '+5541999991111' in payload and '540520.00' in payload
    assert payments.crc16(payload[:-4])==payload[-4:]
    eid=create_event('paid','missing-provider')
    client=member_client('provider-missing@example.com');page=client.get('/evento/missing-provider')
    result=client.post('/evento/missing-provider/participar',data={'csrf':token(page)},follow_redirects=False);url=result.headers['location'];page=client.get(url)
    assert 'Nenhuma cobrança foi gerada' in page.text
    assert client.post(url+'/pagar',data={'csrf':token(page)}).status_code==503
    with SessionLocal() as db:assert db.get(Registration,int(url.split('/')[-1])).payment_status=='pending'
    client.close()


def test_f_additive_migration_preserves_old_records(tmp_path,monkeypatch):
    from sqlalchemy import create_engine, inspect
    from app import migrations
    old=create_engine('sqlite:///'+str(tmp_path/'old.db'))
    with old.begin() as db:
        db.exec_driver_sql('CREATE TABLE admins (id INTEGER PRIMARY KEY, email VARCHAR(254), password_hash VARCHAR(200), created_at TIMESTAMP)')
        db.exec_driver_sql('CREATE TABLE events (id INTEGER PRIMARY KEY, title VARCHAR(160))')
        db.exec_driver_sql('CREATE TABLE registrations (id INTEGER PRIMARY KEY, event_id INTEGER, full_name VARCHAR(140), phone VARCHAR(22), status VARCHAR(20))')
        db.exec_driver_sql("INSERT INTO admins (id,email,password_hash) VALUES (7,'legacy@example.com','old-hash')")
        db.exec_driver_sql("INSERT INTO events VALUES (2,'Evento legado')")
        db.exec_driver_sql("INSERT INTO registrations VALUES (8,2,'Pessoa Legado',NULL,'confirmed')")
    monkeypatch.setattr(migrations,'engine',old)
    migrations.upgrade();migrations.upgrade()
    with old.connect() as db:
        assert db.exec_driver_sql('SELECT title,mode FROM events WHERE id=2').one()==('Evento legado','list')
        assert db.exec_driver_sql('SELECT full_name,status,member_id FROM registrations WHERE id=8').one()==('Pessoa Legado','confirmed',None)
        assert db.exec_driver_sql('SELECT is_superadmin FROM admins WHERE id=7').scalar()==1
        assert 'site_images' in inspect(db).get_table_names()
    old.dispose()
