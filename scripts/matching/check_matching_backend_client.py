"""Run the unchanged backend Java client against a temporary real AI HTTP server.

Requires a JDK and the backend's dependency JARs already in Gradle's local cache.
No Spring application, backend DB, Redis or paid provider is started.
"""
import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
import zipfile

import httpx
import uvicorn
import sys

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from ai_service.core.config import Settings
from ai_service.main import create_app

SOURCES=[
 'domain/user/entity/User.java','domain/travel/entity/TravelPaceType.java','domain/travel/entity/TravelThemeType.java',
 'domain/matching/entity/MatchingRequest.java','domain/matching/entity/MatchingRequestTheme.java',
 'domain/matching/entity/MatchingProfile.java','domain/matching/entity/MatchingProfileTheme.java',
 'domain/matching/entity/MatchingGender.java','domain/matching/entity/PreferredCompanionGender.java',
 'domain/matching/dto/MatchingCandidateAiRequest.java','domain/matching/dto/MatchingAiCandidatesResponse.java',
 'domain/matching/dto/MatchingAiCandidateResponse.java','domain/matching/service/DefaultMatchingAiClient.java',
 'domain/matching/service/MatchingAiClient.java','domain/matching/config/MatchingAiProperties.java',
 'domain/travel/config/AiServerProperties.java','global/error/BusinessException.java','global/error/ErrorCode.java',
]
DEPENDENCIES=[
 ('org.springframework',m,'7.0.7') for m in ('spring-web','spring-core','spring-beans','spring-context','spring-expression')
]+[
 ('org.springframework.boot','spring-boot','4.0.6'),
 ('tools.jackson.core','jackson-core','3.1.2'),('tools.jackson.core','jackson-databind','3.1.2'),
 ('com.fasterxml.jackson.core','jackson-annotations','2.21'),
 ('jakarta.persistence','jakarta.persistence-api','3.2.0'),
 ('io.micrometer','micrometer-observation','1.16.5'),('io.micrometer','micrometer-commons','1.16.5'),
 ('org.slf4j','slf4j-api','2.0.17'),('commons-logging','commons-logging','1.3.5'),
]
HARNESS='''package com.audigo.domain.matching.service;
import com.audigo.domain.matching.config.MatchingAiProperties;
import com.audigo.domain.travel.config.AiServerProperties;
import com.audigo.domain.travel.entity.TravelThemeType;
import com.audigo.domain.travel.entity.TravelPaceType;
import com.audigo.domain.matching.entity.MatchingGender;
import com.audigo.domain.matching.entity.PreferredCompanionGender;
import com.audigo.domain.matching.dto.MatchingCandidateAiRequest;
import com.audigo.domain.matching.dto.MatchingAiCandidatesResponse;
import org.springframework.web.client.RestClient;
import java.util.List;
public class BackendMatchingSmoke {
 static MatchingCandidateAiRequest.CandidateProfilePayload candidate(long id,String nick,MatchingGender gender) {
   return new MatchingCandidateAiRequest.CandidateProfilePayload(id,nick,gender,
     List.of(TravelThemeType.NATURE,TravelThemeType.FOOD),TravelPaceType.BALANCED);
 }
 public static void main(String[] args) {
   var props=new AiServerProperties(args[0],null,"contract-smoke-only",5,30,30);
   var client=new DefaultMatchingAiClient(RestClient.builder(),props,new MatchingAiProperties("/api/ai/v1/matching-requests",false,3));
   var condition=new MatchingCandidateAiRequest.RequestPayload(10L,1L,PreferredCompanionGender.FEMALE,
     List.of(TravelThemeType.NATURE,TravelThemeType.FOOD),TravelPaceType.BALANCED,100000,800000);
   var response=client.getCandidates(new MatchingCandidateAiRequest(condition,List.of(
     candidate(22,"매칭후보A",MatchingGender.MALE),candidate(23,"매칭후보B",MatchingGender.FEMALE))));
   if(response.count()!=1 || response.candidates().getFirst().userId()!=23L) throw new AssertionError("Wrong recommendation");
   int score=response.candidates().getFirst().matchRate();
   if(score<0 || score>100) throw new AssertionError("Invalid integer match_rate");
   var empty=client.getCandidates(new MatchingCandidateAiRequest(condition,List.of()));
   if(empty.count()!=0 || !empty.candidates().isEmpty()) throw new AssertionError("Invalid empty result");
   var ties=client.getCandidates(new MatchingCandidateAiRequest(condition,List.of(
     candidate(44,"나",MatchingGender.FEMALE),candidate(33,"가",MatchingGender.FEMALE),
     candidate(55,"다",MatchingGender.FEMALE),candidate(66,"가",MatchingGender.FEMALE),candidate(1,"본인",MatchingGender.FEMALE))));
   if(!ties.candidates().stream().map(c->c.userId()).toList().equals(List.of(33L,66L,44L))) throw new AssertionError("Invalid nickname ties");
   System.out.println("JAVA_CLIENT_OK count="+response.count()+" user_id=23 match_rate="+score+" empty_count=0 tie_ids=33,66,44");
 }
}'''


def run(source,cache,output):
    jars=[]
    for group,module,version in DEPENDENCIES:
        found=list((cache/group/module/version).glob(f'*/{module}-{version}.jar'))
        if not found: raise RuntimeError('Missing cached backend dependency: '+module+' '+version)
        jars.append(str(found[0]))
    javac=shutil.which('javac');java=shutil.which('java')
    if not javac or not java: raise RuntimeError('JDK not installed')
    with TemporaryDirectory(prefix='audigo-java-client-') as directory:
        folder=Path(directory);files=[]
        with zipfile.ZipFile(source) as archive:
            prefix=next(n for n in archive.namelist() if n.endswith('src/main/java/com/audigo/'+SOURCES[0]))
            prefix=prefix[:-len(SOURCES[0])]
            commit=archive.comment.decode().strip()
            for rel in SOURCES:
                path=folder/'src'/rel
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(archive.read(prefix+rel));files.append(str(path))
        harness=folder/'BackendMatchingSmoke.java';harness.write_text(HARNESS);files.append(str(harness))
        classes=folder/'classes';classes.mkdir()
        classpath=':'.join(jars)
        compiled=subprocess.run([javac,'--release','21','-cp',classpath,'-d',str(classes),*files],capture_output=True,text=True,timeout=30)
        if compiled.returncode: raise RuntimeError('Backend client compilation failed:\n'+compiled.stderr)
        captured=[]
        app=create_app(settings=Settings(api_token='contract-smoke-only'))
        @app.middleware('http')
        async def capture(request,call_next):
            payload=await request.json() if request.method=='POST' else None
            response=await call_next(request)
            if request.method=='POST': captured.append({'payload':payload,'http_status':response.status_code})
            return response
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                try:
                    if httpx.get(f'http://127.0.0.1:{port}/health',timeout=.5).status_code==200:break
                except httpx.HTTPError:time.sleep(.05)
            else:raise RuntimeError('Temporary AI server failed to start')
            result=subprocess.run([java,'-cp',str(classes)+':'+classpath,
                'com.audigo.domain.matching.service.BackendMatchingSmoke',f'http://127.0.0.1:{port}'],
                capture_output=True,text=True,timeout=30)
            if result.returncode:raise RuntimeError('Backend client HTTP test failed:\n'+result.stderr+'\n'+result.stdout)
            if len(captured)!=3 or any(r['http_status']!=200 for r in captured):raise RuntimeError('Expected three final HTTP 200 responses')
            report={'backend_commit':commit,'unchanged_backend_java_client':True,'real_http':True,
                    'ranking_version':'theme-pace-v1','matching_path':'/api/ai/v1/matching-requests','local_e5':False,'database_required':False,'redis_required':False,
                    'cases':captured,'java_result':result.stdout.strip(),
                    'scope':'Original Java client/DTO HTTP integration; full Spring application and production deployment not started'}
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            print(result.stdout.strip())
        finally:
            server.should_exit=True;thread.join(timeout=10)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--backend-source',type=Path,required=True)
    parser.add_argument('--gradle-cache',type=Path,default=Path.home()/'.gradle/caches/modules-2/files-2.1')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/matching/theme-pace-v1/backend-client-smoke.json')
    args=parser.parse_args()
    run(args.backend_source,args.gradle_cache,args.output)
