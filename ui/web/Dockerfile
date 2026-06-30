FROM nginx:alpine

# Download pdf.js at build time so it's served locally (no CDN needed at runtime)
RUN apk add --no-cache curl && \
    curl -fsSL -o /usr/share/nginx/html/pdf.min.js \
      https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js && \
    curl -fsSL -o /usr/share/nginx/html/pdf.worker.min.js \
      https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js && \
    apk del curl

COPY index.html  /usr/share/nginx/html/index.html
COPY nginx.conf  /etc/nginx/conf.d/default.conf
EXPOSE 80
