//! Exercise both handshake directions against a real, isolated NATS server.
use super::*;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use wrpc_transport::{Invoke, Serve};

#[test]
fn concurrent_handshakes_preserve_fast_replies() {
    tokio::runtime::Runtime::new().unwrap().block_on(async {
        tokio::time::timeout(std::time::Duration::from_secs(30), async {
            let url = std::env::var("REGISTRATION_TEST_NATS_URL")
                .expect("set REGISTRATION_TEST_NATS_URL to an isolated NATS server");
            let nats = Arc::new(async_nats::connect(url).await.unwrap());
            let prefix = format!("registration-test.{}", nuid::next());
            let caller = Client::new(Arc::clone(&nats), prefix.clone(), None).await.unwrap();
            let callee = Client::new(Arc::clone(&nats), prefix, None).await.unwrap();
            let invocations = callee.serve("test", "echo", Vec::<Box<[Option<usize>]>>::new())
                .await.unwrap();
            nats.flush().await.unwrap();
            let server = tokio::spawn(invocations.take(3000).for_each_concurrent(100, |invocation| async {
                let (_, mut outgoing, mut incoming) = invocation.unwrap();
                let mut data = [0; 8];
                incoming.read_exact(&mut data).await.unwrap();
                outgoing.write_all(&data).await.unwrap();
                outgoing.shutdown().await.unwrap();
            }));
            let mut callers = JoinSet::new();
            for worker in 0..100u64 {
                let caller = caller.clone();
                callers.spawn(async move {
                    for iteration in 0..30u64 {
                        let expected = (worker * 30 + iteration).to_le_bytes();
                        let (mut outgoing, mut incoming) = caller.invoke(
                            None, "test", "echo", Bytes::copy_from_slice(&expected),
                            Vec::<Vec<Option<usize>>>::new(),
                        ).await.unwrap();
                        outgoing.shutdown().await.unwrap();
                        let mut actual = [0; 8];
                        incoming.read_exact(&mut actual).await.unwrap();
                        assert_eq!(actual, expected);
                    }
                });
            }
            while let Some(result) = callers.join_next().await { result.unwrap(); }
            server.await.unwrap();
        }).await.expect("registration stress test exceeded 30 seconds");
    });
}
