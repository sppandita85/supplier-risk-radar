// CI/CD for Supplier Risk Radar.
// Pull requests: build test image -> lint -> unit tests -> integration tests (Postgres) -> build runtime image -> smoke test.
// main branch:   all of the above, then push the image to ghcr.io.
// Everything runs in Docker (the agent needs the docker CLI and access to a Docker daemon).

pipeline {
  agent any

  options {
    timestamps()
    timeout(time: 30, unit: 'MINUTES')
    disableConcurrentBuilds(abortPrevious: true)
    buildDiscarder(logRotator(numToKeepStr: '20'))
  }

  environment {
    IMAGE = 'ghcr.io/sppandita85/supplier-risk-radar'
  }

  stages {
    stage('Prepare') {
      steps {
        script {
          def ref = env.CHANGE_ID ? "pr-${env.CHANGE_ID}" : env.BRANCH_NAME.replaceAll('[^A-Za-z0-9_.-]', '-')
          env.TAG = "${ref}-${env.BUILD_NUMBER}".toLowerCase()
          env.CI_ID = "radar-ci-${env.TAG}"            // prefix for throwaway containers / network
          env.GIT_SHORT = sh(script: 'git rev-parse --short HEAD', returnStdout: true).trim()
        }
        sh 'docker version --format "Docker {{.Server.Version}}"'
        sh 'rm -rf reports && mkdir -p reports'
        echo "Building ${env.CHANGE_ID ? "PR #${env.CHANGE_ID} (${env.CHANGE_BRANCH} -> ${env.CHANGE_TARGET})" : env.BRANCH_NAME} as ${env.TAG}"
      }
    }

    stage('Build test image') {
      steps {
        sh 'docker build --target test -t radar-test:$TAG .'
      }
    }

    stage('Lint') {
      steps {
        sh 'docker run --rm radar-test:$TAG ruff check . --output-format concise'
      }
    }

    stage('Unit tests') {
      steps {
        sh '''
          docker run --name $CI_ID-unit radar-test:$TAG \
            python -m pytest -m "not integration" --junitxml=/app/reports/unit.xml -q
        '''
      }
      post {
        always {
          sh 'docker cp $CI_ID-unit:/app/reports/unit.xml reports/unit.xml || true'
          sh 'docker rm -f $CI_ID-unit || true'
        }
      }
    }

    stage('Integration tests') {
      steps {
        sh '''
          docker network create $CI_ID-net
          docker run -d --name $CI_ID-pg --network $CI_ID-net \
            -e POSTGRES_USER=radar -e POSTGRES_PASSWORD=radar -e POSTGRES_DB=radar_test postgres:16-alpine
          for i in $(seq 1 30); do
            docker exec $CI_ID-pg pg_isready -U radar -d radar_test >/dev/null 2>&1 && break
            sleep 2
          done
          docker run --name $CI_ID-int --network $CI_ID-net \
            -e PG_DSN="host=$CI_ID-pg dbname=radar_test user=radar password=radar" \
            radar-test:$TAG python -m pytest -m integration --junitxml=/app/reports/integration.xml -q
        '''
      }
      post {
        always {
          sh 'docker cp $CI_ID-int:/app/reports/integration.xml reports/integration.xml || true'
          sh 'docker rm -f $CI_ID-int $CI_ID-pg || true'
        }
      }
    }

    stage('Build runtime image') {
      steps {
        sh 'docker build -t $IMAGE:$TAG -t $IMAGE:$GIT_SHORT .'
      }
    }

    stage('Smoke test image') {
      steps {
        sh '''
          docker run -d --name $CI_ID-db --network $CI_ID-net \
            -e POSTGRES_USER=radar -e POSTGRES_PASSWORD=radar -e POSTGRES_DB=risk_radar postgres:16-alpine
          docker run -d --name $CI_ID-app --network $CI_ID-net -e TRACING=off \
            -e PG_DSN="host=$CI_ID-db dbname=risk_radar user=radar password=radar" $IMAGE:$TAG
          for i in $(seq 1 45); do
            status=$(docker inspect -f '{{.State.Health.Status}}' $CI_ID-app 2>/dev/null || echo missing)
            [ "$status" = "healthy" ] && break
            [ "$status" = "unhealthy" ] && break
            sleep 2
          done
          echo "dashboard container health: $status"
          docker exec $CI_ID-app python -c "
from radar.db import connect
with connect() as c:
    n = c.execute('select count(*) as n from suppliers').fetchone()['n']
assert n == 5, n
print('schema ok, suppliers =', n)"
          [ "$status" = "healthy" ] || { docker logs $CI_ID-app; exit 1; }
        '''
      }
      post {
        always {
          sh 'docker rm -f $CI_ID-app $CI_ID-db || true'
        }
      }
    }

    stage('Push image') {
      when { branch 'main' }
      steps {
        withCredentials([usernamePassword(credentialsId: 'ghcr', usernameVariable: 'GHCR_USER',
                                          passwordVariable: 'GHCR_TOKEN')]) {
          // Rebuild for amd64 + arm64 so :latest stays multi-arch (layers come from cache)
          sh '''
            echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
            docker buildx inspect radar-ci-builder >/dev/null 2>&1 || \
              docker buildx create --name radar-ci-builder --driver docker-container >/dev/null
            docker buildx build --builder radar-ci-builder --platform linux/amd64,linux/arm64 \
              -t $IMAGE:$GIT_SHORT -t $IMAGE:latest --push .
            docker logout ghcr.io
          '''
        }
      }
    }
  }

  post {
    always {
      junit allowEmptyResults: true, testResults: 'reports/*.xml'
      sh '''
        docker rm -f $CI_ID-unit $CI_ID-int $CI_ID-pg $CI_ID-app $CI_ID-db >/dev/null 2>&1 || true
        docker network rm $CI_ID-net >/dev/null 2>&1 || true
        docker rmi radar-test:$TAG >/dev/null 2>&1 || true
      '''
    }
  }
}
