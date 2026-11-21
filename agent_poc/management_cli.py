"""Fake-only MCP registration console. No server subprocess or LLM is started."""
import argparse
import asyncio
from getpass import getpass
import json
from pathlib import Path

from .mcp_management import MCPManagement
from .owner_runtime import OwnerFakeRuntime
from .readiness_gate import ReadOnlyScope, aligned_read_only_scope
from mcp_manager.models import ToolInfo


def show(value):print(json.dumps(value,ensure_ascii=False,default=str,indent=2))


def confirm(review):
    show(review)
    return input('위 범위에 동의합니까? y=허용 / 나머지=거부: ').strip().lower()=='y'


def arguments_form(schema):
    """Basic schema form; complex schemas use an explicit advanced JSON input."""
    properties=schema.get('properties',{})
    if any(s.get('type') not in ('string','number','integer','boolean') for s in properties.values()):
        return json.loads(input('복합 Schema: 고급 JSON 인수 입력: '))
    result={}
    for name,field in properties.items():
        required=name in schema.get('required',[])
        label=f'{name} ({field.get("type")}, 필수={required}'
        if 'default' in field:label+=f', 기본={field["default"]}'
        if 'enum' in field:label+=f', 선택={field["enum"]}'
        value=input(label+'): ')
        if not value and not required:continue  # SDK/schema default, never coercion
        kind=field['type']
        if kind=='integer':value=int(value)
        elif kind=='number':value=float(value)
        elif kind=='boolean':
            if value.lower() not in ('true','false'):raise ValueError('true/false를 입력하세요')
            value=value.lower()=='true'
        result[name]=value
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,required=True)
    parser.add_argument('--fixture-kind',choices=('generic','time','filesystem'),default='generic')
    args=parser.parse_args()
    runtime=OwnerFakeRuntime(prepared=args.fixture_kind!='generic')
    if args.fixture_kind=='time':
        runtime.tools=[ToolInfo('get_current_time','FAKE time',{'type':'object','properties':{
            'timezone':{'type':'string','default':'Asia/Seoul'}}})]
    elif args.fixture_kind=='filesystem':
        runtime.tools=[ToolInfo(name,'FAKE filesystem',{'type':'object','properties':{
            'path':{'type':'string'}},'required':['path']}) for name in ('list_directory','read_file')]
    def binder(runtime,config,options):
        if args.fixture_kind=='generic':return None
        scope=aligned_read_only_scope(ReadOnlyScope(config.server_id,args.fixture_kind,
            Path(options['root']) if options['root'] else None),config)
        runtime.manager._client_factory.bind(scope)
        return scope
    manager=None
    try:
        manager=MCPManagement(args.base,runtime_factory=lambda:runtime,contract_binder=binder)
        print('FAKE 전용: 실제 서버/파일/시각/모델을 조회하지 않습니다. 결과는 고정 Fixture입니다.')
        while True:
            print('1 목록  2 등록  3 상세  4 연결 동의/Discovery  5 도구/권한  6 승인 모드')
            print('7 신뢰/철회  8 Tool 테스트  9 감사/정책  10 중지  11 삭제  12 테스트 상태/취소  0 종료')
            try:
                command=input('선택: ').strip()
                if command=='0':break
                if command=='1':show(manager.list_servers());continue
                if command=='2':
                    name=input('표시 이름: ');exe=input('실행 파일/명령: ');argv=[];env={}
                    print('인수를 하나씩 입력하세요. 빈 줄=종료, 따옴표로 감싸지 마세요.')
                    while True:
                        item=input('인수: ')
                        if not item:break
                        argv.append(item)
                    cwd=input('cwd (생략 가능): ') or None;root=input('작업 Root (생략 가능): ') or None
                    while True:
                        key=input('환경변수 이름 (빈 줄=종료): ')
                        if not key:break
                        env[key]=getpass('값 (화면에 표시하지 않음): ')
                    timeout=float(input('호출 대기 초 (기본30): ') or '30')
                    mode=input('서버 승인 모드 (빈 줄=프로젝트 기본): ') or None
                    show(manager.add_server(name,exe,argv,cwd=cwd,root=root,env=env,timeout=timeout,mode=mode,confirm=confirm))
                    continue
                if command=='9':show({'policy':manager.policy.snapshot(),'audit':manager.audit()});continue
                if command=='12':
                    tid=input('Test ID: ');show(manager.test_status(tid))
                    if input('취소 요청? y/기타: ').lower()=='y':show(manager.cancel_test(tid))
                    continue
                if command not in ('3','4','5','6','7','8','10','11'):continue
                sid=input('Server ID: ')
                if command=='3':show(manager.details(sid))
                elif command=='4':show(manager.test_connection(sid,confirm=confirm))
                elif command=='5':
                    show(manager.tools(sid));tool=input('권한 변경 Tool 이름 (빈 줄=보기만): ')
                    if tool:show(manager.set_permission(sid,tool,input('allow/block: ').lower()=='allow'))
                elif command=='6':
                    mode=input('ASK_EVERY_TIME / AUTO_ALLOW_SAFE / ALLOW_ALL: ')
                    enable=input('자동 승인 활성화에 명시 동의? y/기타: ').lower()=='y'
                    manager.set_mode(mode,server_id=sid,enable_auto=enable)
                elif command=='7':
                    show(manager.details(sid)['trusts']);revoke=input('철회할 Trust ID (빈 줄=새 신뢰): ')
                    if revoke:manager.revoke_trust(revoke)
                    else:
                        tool=input('Tool 이름: ');resource=input('신뢰 Root/파일 (Time은 빈 줄): ') or None
                        safe=input('읽기 전용 동작을 별도 검토했습니까? y/기타: ').lower()=='y'
                        show(manager.trust(sid,tool,resource=resource,verified_read_only=safe,confirm=confirm))
                elif command=='8':
                    tool=input('Tool 이름: ');entry=manager.entry(sid,tool)
                    values=arguments_form(entry.descriptor['input_schema'])
                    prepared=manager.prepare_tool_test(sid,tool,values);show(prepared)
                    show(asyncio.run(manager.run_tool_test(prepared['test_id'],confirm=confirm)))
                elif command=='10':show(manager.stop_server(sid))
                elif command=='11':
                    if confirm({'purpose':'DELETE_STOPPED_REGISTRATION','server_id':sid}):show(manager.delete_server(sid))
            except (EOFError,KeyboardInterrupt):break
            except Exception as error:
                # Arbitrary upstream messages can contain args/env; code/type only.
                print('실패:',getattr(error,'code',type(error).__name__))
    finally:
        try:
            if manager is not None:manager.shutdown()
        finally:runtime.shutdown()


if __name__=='__main__':main()
