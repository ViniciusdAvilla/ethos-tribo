"""Live feature checks with isolated synthetic activities; never charge real money."""
import os
import secrets
from urllib.parse import urlsplit,urlunsplit
import httpx
import psycopg
from bs4 import BeautifulSoup
from PIL import Image
import io

password=os.environ.get('TEST_ADMIN_PASSWORD')
if not password or os.environ.get('LIVE_FEATURE_TESTS')!='true':
    print('Live feature smoke skipped; isolated PostgreSQL suite remains active.');raise SystemExit(0)
base=os.environ['BASE_URL']
parts=urlsplit(os.environ['DATABASE_URL'])
direct=urlunsplit((parts.scheme,parts.netloc.replace('-pooler.','.'),parts.path,parts.query,''))
marker='Teste funcional '+secrets.token_hex(8)
email='ethos-features-'+secrets.token_hex(10)+'@example.invalid'
member_password=secrets.token_urlsafe(24)
created_events=[]

def csrf(response):
    item=BeautifulSoup(response.text,'html.parser').select_one('input[name="csrf"]')
    assert item,'CSRF token missing';return item['value']

with httpx.Client(base_url=base,timeout=90,follow_redirects=False) as admin,httpx.Client(base_url=base,timeout=90,follow_redirects=False) as member,httpx.Client(base_url=base,timeout=90,follow_redirects=False) as scoped:
    try:
        page=admin.get('/admin/entrar')
        assert admin.post('/admin/entrar',data={'csrf':csrf(page),'email':os.environ['ADMIN_EMAIL'],'password':password}).status_code==303
        photos=admin.get('/admin/fotos');assert photos.status_code==200 and len(BeautifulSoup(photos.text,'html.parser').select('.photo-editor'))>=9
        assert admin.get('/admin/pix').status_code==200
        for mode in ['free','paid','list']:
            form=admin.get('/admin/eventos/novo')
            response=admin.post('/admin/eventos/novo',data={'csrf':csrf(form),'title':marker+' '+mode,'description':'Atividade sintética de validação; será removida ao terminar','event_date':'2020-01-01','starts_at':'15:00','ends_at':'17:00','location_name':'Local de teste','address':'Curitiba','capacity':'8','price':'2,00','mode':mode,'is_published':'yes','checkin_enabled':'yes','followup_url':'https://example.com/acompanhamento','extra_links':'Grupo | https://example.com/grupo'})
            assert response.status_code==303
            event_id=int(response.headers['location'].split('/')[-1]);created_events.append(event_id)
        with psycopg.connect(direct) as db:
            rows=db.execute('SELECT id,slug,mode FROM events WHERE id=ANY(%s)',(created_events,)).fetchall()
            events={row[2]:{'id':row[0],'slug':row[1]} for row in rows}
        page=member.get('/conta/cadastrar')
        assert member.post('/conta/cadastrar',data={'csrf':csrf(page),'full_name':marker,'email':email,'password':member_password,'consent':'yes'}).status_code==303
        paths={}
        for mode in ['free','paid']:
            page=member.get('/evento/'+events[mode]['slug'])
            assert 'name="full_name"' not in page.text and 'name="phone"' not in page.text
            response=member.post('/evento/'+events[mode]['slug']+'/participar',data={'csrf':csrf(page)})
            assert response.status_code==303;paths[mode]=response.headers['location']
            with psycopg.connect(direct) as db:
                record=db.execute('SELECT full_name,email,payment_status FROM registrations WHERE id=%s',(int(paths[mode].split('/')[-1]),)).fetchone()
                assert record==(marker,email,'exempt' if mode=='free' else 'pending')
        free=member.get(paths['free']);assert 'Participação confirmada!' in free.text
        assert member.post(paths['free']+'/check-in',data={'csrf':csrf(free),'image_consent':'yes'}).status_code==303
        free=member.get(paths['free']);assert 'Revogar autorização de imagem' in free.text
        assert member.post(paths['free']+'/revogar-imagem',data={'csrf':csrf(free)}).status_code==303
        with psycopg.connect(direct) as db:
            record=db.execute('SELECT attended,checked_in_at,image_consent_at,image_revoked_at,image_consent_text FROM registrations WHERE id=%s',(int(paths['free'].split('/')[-1]),)).fetchone()
            assert all(record[:4]) and 'Versão 1' in record[4]
        paid=member.get(paths['paid']);assert 'Nenhuma cobrança foi gerada' in paid.text
        assert member.post(paths['paid']+'/pagar',data={'csrf':csrf(paid)}).status_code==503
        assert member.post(paths['paid']+'/check-in',data={'csrf':csrf(paid),'image_consent':'yes'}).status_code==409
        print('PUBLIC FEATURE PASS: automatic account data, free participation, saved tracking, consent and revocation, unpaid access blocked')
        # List mode has the requested static QR but remains explicitly manually confirmed.
        page=member.get('/evento/'+events['list']['slug'])
        response=member.post('/evento/'+events['list']['slug']+'/inscrever',data={'csrf':csrf(page),'full_name':marker,'phone':'419'+str(secrets.randbelow(100000000)).zfill(8),'email':email,'consent':'yes'})
        assert response.status_code==303;list_path=response.headers['location'];page=member.get(list_path)
        assert 'Pix Copia e Cola' in page.text
        assert Image.open(io.BytesIO(member.get(list_path+'/qr.png').content)).width>200
        with psycopg.connect(direct) as db:
            registration=db.execute('SELECT id FROM registrations WHERE event_id=%s AND email=%s',(events['list']['id'],email)).fetchone()[0]
        page=admin.get('/admin/eventos/'+str(events['list']['id']))
        assert admin.post('/admin/inscricoes/'+str(registration)+'/atualizar',data={'csrf':csrf(page),'payment_status':'paid'}).status_code==303
        assert 'Pagamento concluído!' in member.get(list_path).text
        report=admin.get('/admin/eventos/'+str(events['list']['id'])+'/pagamentos')
        assert marker in report.text and 'Conferência manual' in report.text
        png=admin.get('/admin/eventos/'+str(events['list']['id'])+'/pagamentos.png?heading=ETHOS%20TESTE&theme=verde')
        assert png.status_code==200 and Image.open(io.BytesIO(png.content)).width==1200
        with psycopg.connect(direct) as db:
            assert db.execute('SELECT paid_at FROM registrations WHERE id=%s',(registration,)).fetchone()[0]
        print('PUBLIC FEATURE PASS: static Pix QR and copy/paste, payment timestamp and custom PNG export')
        page=admin.get('/admin/responsaveis')
        assert admin.post('/admin/responsaveis',data={'csrf':csrf(page),'email':email,'event_ids':[str(events['free']['id']),str(events['paid']['id'])]}).status_code==303
        page=scoped.get('/admin/entrar')
        assert scoped.post('/admin/entrar',data={'csrf':csrf(page),'email':email,'password':member_password}).status_code==303
        dashboard=scoped.get('/admin');assert dashboard.status_code==200
        assert marker+' free' in dashboard.text and marker+' paid' in dashboard.text and marker+' list' not in dashboard.text
        assert scoped.get('/admin/eventos/'+str(events['free']['id'])+'/editar').status_code==200
        assert scoped.get('/admin/eventos/'+str(events['list']['id'])).status_code==403
        assert scoped.get('/admin/eventos/1').status_code==403
        for path in ['/admin/fotos','/admin/responsaveis','/admin/pix','/admin/eventos/novo']:
            assert scoped.get(path).status_code==403
        print('PUBLIC FEATURE PASS: one administrator on multiple activities, server-side isolation, global settings protected')
    finally:
        # Delete only exact synthetic records created by this invocation, in FK order.
        with psycopg.connect(direct) as db:
            db.execute('DELETE FROM event_admins WHERE admin_id IN (SELECT id FROM admins WHERE email=%s AND is_superadmin=FALSE)',(email,))
            db.execute('DELETE FROM admins WHERE email=%s AND is_superadmin=FALSE',(email,))
            db.execute('DELETE FROM registrations WHERE email=%s AND full_name=%s',(email,marker))
            db.execute('DELETE FROM members WHERE email=%s AND full_name=%s',(email,marker))
            if created_events:
                db.execute('DELETE FROM events WHERE id=ANY(%s) AND title LIKE %s',(created_events,marker+'%'))
        print('PUBLIC FEATURE CLEANUP: synthetic activities, accounts and grants removed')
